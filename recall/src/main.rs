mod app;
mod db;
mod models;
mod ui;

use std::{io, path::PathBuf};

use anyhow::Result;
use clap::{Parser, Subcommand};
use crossterm::{
    event::{
        self, DisableMouseCapture, EnableMouseCapture, Event, KeyCode, KeyModifiers,
    },
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use ratatui::{backend::CrosstermBackend, Terminal};

use app::{App, AppState, FormField, FormState};
use db::Database;
use models::Category;

// ─── CLI ─────────────────────────────────────────────────────────────────────

#[derive(Parser)]
#[command(name = "recall", about = "Terminal knowledge base with fuzzy search")]
struct Cli {
    #[command(subcommand)]
    command: Option<Commands>,
}

#[derive(Subcommand)]
enum Commands {
    /// Add an entry directly from the command line
    Add {
        #[arg(short, long)]
        title: String,
        #[arg(short, long, default_value = "")]
        content: String,
        #[arg(short = 'C', long, default_value = "note")]
        category: String,
        #[arg(long, default_value = "")]
        tags: String,
    },
    /// Quick search (prints results to stdout)
    Search {
        query: String,
    },
    /// Print the path to the database file
    DbPath,
}

// ─── Entry point ─────────────────────────────────────────────────────────────

fn main() -> Result<()> {
    let cli     = Cli::parse();
    let db_path = recall_db_path();
    let db      = Database::new(&db_path)?;

    match cli.command {
        Some(Commands::Add { title, content, category, tags }) => {
            let cat = Category::from_str(&category);
            let tag_vec: Vec<String> = tags
                .split(',')
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .collect();
            db.add_entry(&title, &content, cat, &tag_vec)?;
            println!("Added: {}", title);
        }

        Some(Commands::Search { query }) => {
            let entries = db.search_plain(&query)?;
            if entries.is_empty() {
                println!("No results for: {}", query);
            } else {
                for e in &entries {
                    println!("[{}] {}  |  {}", e.category.label(), e.title, e.tags_display());
                }
            }
        }

        Some(Commands::DbPath) => {
            println!("{}", db_path.display());
        }

        None => {
            // Launch TUI
            run_tui(db)?;
        }
    }

    Ok(())
}

fn recall_db_path() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    let dir  = PathBuf::from(home).join(".local").join("share").join("recall");
    std::fs::create_dir_all(&dir).ok();
    dir.join("recall.db")
}

// ─── TUI event loop ───────────────────────────────────────────────────────────

fn run_tui(db: Database) -> Result<()> {
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen, EnableMouseCapture)?;

    let backend  = CrosstermBackend::new(stdout);
    let mut term = Terminal::new(backend)?;
    let mut app  = App::new(db)?;

    loop {
        term.draw(|f| ui::render(f, &mut app))?;

        if event::poll(std::time::Duration::from_millis(50))? {
            if let Event::Key(key) = event::read()? {
                // ── Open $EDITOR for content field ────────────────────────
                let in_form = app.state == AppState::AddForm || app.state == AppState::EditForm;
                if in_form
                    && key.modifiers.contains(KeyModifiers::CONTROL)
                    && key.code == KeyCode::Char('o')
                {
                    disable_raw_mode()?;
                    execute!(term.backend_mut(), LeaveAlternateScreen, DisableMouseCapture)?;

                    let editor   = std::env::var("EDITOR").unwrap_or_else(|_| "nano".to_string());
                    let tmp_path = std::env::temp_dir().join("recall_content_edit.tmp");
                    std::fs::write(&tmp_path, &app.form.content).ok();
                    std::process::Command::new(&editor).arg(&tmp_path).status().ok();
                    if let Ok(text) = std::fs::read_to_string(&tmp_path) {
                        app.form.content = text;
                    }
                    std::fs::remove_file(&tmp_path).ok();

                    enable_raw_mode()?;
                    execute!(term.backend_mut(), EnterAlternateScreen, EnableMouseCapture)?;
                    term.clear()?;
                    continue;
                }

                // Clear status on any keypress
                app.status = None;

                if handle_key(&mut app, key)? {
                    break;
                }
            }
        }
    }

    disable_raw_mode()?;
    execute!(term.backend_mut(), LeaveAlternateScreen, DisableMouseCapture)?;
    term.show_cursor()?;
    Ok(())
}

// ─── Key dispatch ─────────────────────────────────────────────────────────────

fn handle_key(app: &mut App, key: event::KeyEvent) -> Result<bool> {
    match app.state {
        AppState::Browsing      => handle_browsing(app, key),
        AppState::AddForm
        | AppState::EditForm    => handle_form(app, key),
        AppState::ViewFull      => handle_view(app, key),
        AppState::ConfirmDelete => handle_confirm(app, key),
    }
}

// ─── Browsing mode ────────────────────────────────────────────────────────────

fn handle_browsing(app: &mut App, key: event::KeyEvent) -> Result<bool> {
    let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);

    match key.code {
        // Quit
        KeyCode::Char('q') | KeyCode::Char('c') if ctrl => return Ok(true),

        // Navigation
        KeyCode::Up   | KeyCode::Char('k') if ctrl => app.move_up(),
        KeyCode::Down | KeyCode::Char('j') if ctrl => app.move_down(),
        KeyCode::Up                                 => app.move_up(),
        KeyCode::Down                               => app.move_down(),

        // Actions (Ctrl)
        KeyCode::Char('n') if ctrl => {
            app.form  = FormState::new();
            app.state = AppState::AddForm;
        }
        KeyCode::Char('e') if ctrl => {
            if let Some(entry) = app.selected_entry() {
                app.form  = FormState::from_entry(entry);
                app.state = AppState::EditForm;
            }
        }
        KeyCode::Char('x') if ctrl => {
            if app.selected_entry().is_some() {
                app.state = AppState::ConfirmDelete;
            }
        }
        KeyCode::Char('y') if ctrl => {
            app.yank_selected();
        }

        // Full view
        KeyCode::Enter => {
            if app.selected_entry().is_some() {
                app.view_scroll = 0;
                app.state       = AppState::ViewFull;
            }
        }

        // Category filters (F1-F4)
        KeyCode::F(1) => app.set_cat_filter(None),
        KeyCode::F(2) => app.set_cat_filter(Some(Category::Command)),
        KeyCode::F(3) => app.set_cat_filter(Some(Category::Note)),
        KeyCode::F(4) => app.set_cat_filter(Some(Category::Tool)),

        // Search
        KeyCode::Backspace => app.pop_search(),
        KeyCode::Esc       => app.clear_search(),

        // Any printable char → search (when no Ctrl modifier)
        KeyCode::Char(c) if !ctrl => {
            app.push_search(c);
        }

        _ => {}
    }
    Ok(false)
}

// ─── Form mode ────────────────────────────────────────────────────────────────

fn handle_form(app: &mut App, key: event::KeyEvent) -> Result<bool> {
    let ctrl  = key.modifiers.contains(KeyModifiers::CONTROL);
    let shift = key.modifiers.contains(KeyModifiers::SHIFT);

    match key.code {
        KeyCode::Esc => {
            app.state = AppState::Browsing;
        }

        KeyCode::Char('s') if ctrl => {
            app.save_form()?;
        }

        KeyCode::Tab => {
            app.form.focused = app.form.focused.next();
        }
        KeyCode::BackTab => {
            app.form.focused = app.form.focused.prev();
        }

        KeyCode::Enter => {
            match app.form.focused {
                FormField::Content => {
                    app.form.content.push('\n');
                }
                _ => {
                    app.form.focused = app.form.focused.next();
                }
            }
        }

        KeyCode::Left => {
            if app.form.focused == FormField::Category {
                app.form.category = app.form.category.cycle_prev();
            } else {
                // cursor movement — simplified: no-op for now
            }
        }
        KeyCode::Right => {
            if app.form.focused == FormField::Category {
                app.form.category = app.form.category.cycle_next();
            }
        }

        KeyCode::Backspace => match app.form.focused {
            FormField::Title    => { app.form.title.pop(); }
            FormField::Tags     => { app.form.tags.pop(); }
            FormField::Content  => { app.form.content.pop(); }
            FormField::Category => {}
        },

        KeyCode::Char(c) if !ctrl => {
            let ch = if shift { c } else { c };
            match app.form.focused {
                FormField::Title    => app.form.title.push(ch),
                FormField::Tags     => app.form.tags.push(ch),
                FormField::Content  => app.form.content.push(ch),
                FormField::Category => {}
            }
        }

        _ => {}
    }
    Ok(false)
}

// ─── Full-view mode ───────────────────────────────────────────────────────────

fn handle_view(app: &mut App, key: event::KeyEvent) -> Result<bool> {
    let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
    match key.code {
        KeyCode::Esc | KeyCode::Char('q') | KeyCode::Enter => {
            app.state = AppState::Browsing;
        }
        KeyCode::Up   | KeyCode::Char('k') => {
            app.view_scroll = app.view_scroll.saturating_sub(1);
        }
        KeyCode::Down | KeyCode::Char('j') => {
            app.view_scroll += 1;
        }
        KeyCode::Char('y') if ctrl => {
            app.yank_selected();
        }
        _ => {}
    }
    Ok(false)
}

// ─── Confirm-delete mode ──────────────────────────────────────────────────────

fn handle_confirm(app: &mut App, key: event::KeyEvent) -> Result<bool> {
    match key.code {
        KeyCode::Char('y') | KeyCode::Char('Y') => {
            app.delete_selected()?;
        }
        _ => {
            app.state = AppState::Browsing;
        }
    }
    Ok(false)
}
