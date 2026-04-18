use ratatui::{
    layout::{Alignment, Constraint, Direction, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Clear, List, ListItem, ListState, Paragraph, Wrap},
    Frame,
};

use crate::{
    app::{App, AppState, FormField},
    models::Category,
};

// ─── Palette ──────────────────────────────────────────────────────────────────

const C_ACCENT:    Color = Color::Cyan;
const C_DIM:       Color = Color::DarkGray;
const C_WHITE:     Color = Color::White;
const C_CMD:       Color = Color::Green;
const C_NOTE:      Color = Color::Yellow;
const C_TOOL:      Color = Color::Magenta;
const C_WARN:      Color = Color::Red;

fn cat_color(cat: &Category) -> Color {
    match cat {
        Category::Command => C_CMD,
        Category::Note    => C_NOTE,
        Category::Tool    => C_TOOL,
    }
}

// ─── Root render ─────────────────────────────────────────────────────────────

pub fn render(f: &mut Frame, app: &mut App) {
    let area = f.area();
    match app.state {
        AppState::Browsing => {
            render_main(f, app, area);
        }
        AppState::AddForm => {
            render_main(f, app, area);
            render_form(f, app, area, "Add Entry");
        }
        AppState::EditForm => {
            render_main(f, app, area);
            render_form(f, app, area, "Edit Entry");
        }
        AppState::ViewFull => {
            render_view_full(f, app, area);
        }
        AppState::ConfirmDelete => {
            render_main(f, app, area);
            render_confirm(f, area);
        }
    }
}

// ─── Main layout ─────────────────────────────────────────────────────────────

fn render_main(f: &mut Frame, app: &mut App, area: Rect) {
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(3), // search
            Constraint::Min(0),    // body
            Constraint::Length(1), // status
        ])
        .split(area);

    render_search_bar(f, app, rows[0]);

    let cols = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Percentage(36), Constraint::Percentage(64)])
        .split(rows[1]);

    render_list(f, app, cols[0]);
    render_preview(f, app, cols[1]);
    render_statusbar(f, app, rows[2]);
}

fn render_search_bar(f: &mut Frame, app: &App, area: Rect) {
    // Top row: branding + category tabs
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(1), Constraint::Length(2)])
        .split(area);

    // Branding + filter tabs
    let active_style  = Style::default().fg(Color::Black).bg(C_ACCENT).add_modifier(Modifier::BOLD);
    let passive_style = Style::default().fg(C_DIM);

    let tabs = vec![
        (" ALL ", app.cat_filter.is_none()),
        (" CMD ", app.cat_filter == Some(Category::Command)),
        (" NOTE ", app.cat_filter == Some(Category::Note)),
        (" TOOL ", app.cat_filter == Some(Category::Tool)),
    ];

    let mut spans = vec![
        Span::styled("  RECALL ", Style::default().fg(C_ACCENT).add_modifier(Modifier::BOLD)),
        Span::styled("  ", Style::default()),
    ];
    for (label, active) in &tabs {
        spans.push(Span::styled(
            *label,
            if *active { active_style } else { passive_style },
        ));
    }
    spans.push(Span::styled(
        "  [F1-F4 filter]",
        Style::default().fg(C_DIM),
    ));
    f.render_widget(Paragraph::new(Line::from(spans)), rows[0]);

    // Search bar
    let search_block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(C_ACCENT))
        .title(Span::styled(" Search (type to filter) ", Style::default().fg(C_ACCENT)));

    let display = if app.search.is_empty() {
        Span::styled("fuzzy match across title, tags, content ...", Style::default().fg(C_DIM))
    } else {
        Span::styled(format!("{}_", app.search), Style::default().fg(C_WHITE))
    };

    f.render_widget(
        Paragraph::new(Line::from(display)).block(search_block),
        rows[1],
    );
}

fn render_list(f: &mut Frame, app: &mut App, area: Rect) {
    let title = format!(" Entries ({}) ", app.filtered.len());
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(C_DIM))
        .title(Span::styled(title, Style::default().fg(C_WHITE)));

    let items: Vec<ListItem> = app
        .filtered
        .iter()
        .map(|e| {
            let badge = Span::styled(
                format!("[{}] ", e.category.label()),
                Style::default().fg(cat_color(&e.category)),
            );
            let name = Span::styled(e.title.clone(), Style::default().fg(C_WHITE));
            ListItem::new(Line::from(vec![badge, name]))
        })
        .collect();

    let mut state = ListState::default();
    if !app.filtered.is_empty() {
        state.select(Some(app.selected));
    }

    let list = List::new(items)
        .block(block)
        .highlight_style(
            Style::default()
                .bg(Color::Rgb(40, 40, 60))
                .add_modifier(Modifier::BOLD),
        )
        .highlight_symbol("▶ ");

    f.render_stateful_widget(list, area, &mut state);
}

fn render_preview(f: &mut Frame, app: &App, area: Rect) {
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(C_DIM))
        .title(Span::styled(" Preview ", Style::default().fg(C_WHITE)));

    match app.selected_entry() {
        None => {
            f.render_widget(
                Paragraph::new("No entries yet — press Ctrl+N to add one")
                    .block(block)
                    .style(Style::default().fg(C_DIM))
                    .alignment(Alignment::Center),
                area,
            );
        }
        Some(entry) => {
            let cc = cat_color(&entry.category);
            let sep = "─".repeat(area.width.saturating_sub(4) as usize);
            let mut lines = vec![
                Line::from(vec![
                    Span::styled("  Title    ", Style::default().fg(C_DIM)),
                    Span::styled(entry.title.clone(), Style::default().fg(C_WHITE).add_modifier(Modifier::BOLD)),
                ]),
                Line::from(vec![
                    Span::styled("  Category ", Style::default().fg(C_DIM)),
                    Span::styled(entry.category.label(), Style::default().fg(cc)),
                ]),
                Line::from(vec![
                    Span::styled("  Tags     ", Style::default().fg(C_DIM)),
                    Span::styled(entry.tags_display(), Style::default().fg(C_ACCENT)),
                ]),
                Line::from(vec![
                    Span::styled("  Updated  ", Style::default().fg(C_DIM)),
                    Span::styled(entry.updated_at.clone(), Style::default().fg(C_DIM)),
                ]),
                Line::from(Span::styled(sep, Style::default().fg(C_DIM))),
                Line::from(""),
            ];

            for line in entry.content.lines() {
                lines.push(Line::from(Span::styled(
                    format!("  {}", line),
                    Style::default().fg(C_WHITE),
                )));
            }

            f.render_widget(
                Paragraph::new(lines)
                    .block(block)
                    .wrap(Wrap { trim: false }),
                area,
            );
        }
    }
}

fn render_statusbar(f: &mut Frame, app: &App, area: Rect) {
    let text = if let Some(ref msg) = app.status {
        Span::styled(format!("  {}", msg), Style::default().fg(C_ACCENT))
    } else {
        Span::styled(
            "  Ctrl+N add  Ctrl+E edit  Ctrl+X delete  Ctrl+Y yank  Enter view  F1-F4 filter  Ctrl+Q quit",
            Style::default().fg(C_DIM),
        )
    };
    f.render_widget(Paragraph::new(Line::from(text)), area);
}

// ─── Full-screen view ─────────────────────────────────────────────────────────

fn render_view_full(f: &mut Frame, app: &App, area: Rect) {
    if let Some(entry) = app.selected_entry() {
        let cc = cat_color(&entry.category);
        let block = Block::default()
            .borders(Borders::ALL)
            .border_style(Style::default().fg(cc))
            .title(Span::styled(
                format!("  [{}]  {}  ", entry.category.label(), entry.title),
                Style::default().fg(cc).add_modifier(Modifier::BOLD),
            ));

        let sep = "─".repeat(area.width.saturating_sub(4) as usize);
        let mut lines = vec![
            Line::from(vec![
                Span::styled("Tags: ", Style::default().fg(C_DIM)),
                Span::styled(entry.tags_display(), Style::default().fg(C_ACCENT)),
                Span::styled("   ", Style::default()),
                Span::styled("Updated: ", Style::default().fg(C_DIM)),
                Span::styled(entry.updated_at.clone(), Style::default().fg(C_DIM)),
            ]),
            Line::from(Span::styled(sep, Style::default().fg(C_DIM))),
            Line::from(""),
        ];

        for line in entry.content.lines() {
            lines.push(Line::from(Span::styled(line.to_string(), Style::default().fg(C_WHITE))));
        }

        f.render_widget(
            Paragraph::new(lines)
                .block(block)
                .wrap(Wrap { trim: false })
                .scroll((app.view_scroll, 0)),
            area,
        );

        // Bottom hint
        let hint_area = Rect::new(area.x + 2, area.y + area.height - 1, area.width.saturating_sub(4), 1);
        f.render_widget(
            Paragraph::new(Span::styled(
                " Esc back   j/k or ↑↓ scroll   Ctrl+Y copy ",
                Style::default().fg(C_DIM),
            )),
            hint_area,
        );
    }
}

// ─── Add/Edit form popup ──────────────────────────────────────────────────────

fn render_form(f: &mut Frame, app: &App, area: Rect, title: &str) {
    let w = 74u16.min(area.width.saturating_sub(4));
    let h = 24u16.min(area.height.saturating_sub(4));
    let x = (area.width.saturating_sub(w)) / 2;
    let y = (area.height.saturating_sub(h)) / 2;
    let popup = Rect::new(x, y, w, h);

    f.render_widget(Clear, popup);

    let outer = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(C_ACCENT))
        .title(Span::styled(
            format!("  {}  ", title),
            Style::default().fg(C_ACCENT).add_modifier(Modifier::BOLD),
        ));
    f.render_widget(outer, popup);

    let inner = Rect::new(popup.x + 1, popup.y + 1, popup.width.saturating_sub(2), popup.height.saturating_sub(2));

    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(3), // title
            Constraint::Length(3), // category
            Constraint::Length(3), // tags
            Constraint::Min(0),    // content
            Constraint::Length(1), // help
        ])
        .split(inner);

    let form = &app.form;

    // Title field
    input_field(f, rows[0], "Title", &form.title, form.focused == FormField::Title);

    // Category selector
    {
        let active  = form.focused == FormField::Category;
        let bs      = if active { Style::default().fg(C_ACCENT) } else { Style::default().fg(C_DIM) };
        let block   = Block::default().borders(Borders::ALL).border_style(bs).title(" Category  (← →) ");
        let display = format!("  ◀  {}  ▶", form.category.label());
        f.render_widget(
            Paragraph::new(Span::styled(display, Style::default().fg(cat_color(&form.category)))).block(block),
            rows[1],
        );
    }

    // Tags field
    input_field(f, rows[2], "Tags  (comma-separated)", &form.tags, form.focused == FormField::Tags);

    // Content textarea
    {
        let active = form.focused == FormField::Content;
        let bs     = if active { Style::default().fg(C_ACCENT) } else { Style::default().fg(C_DIM) };
        let block  = Block::default()
            .borders(Borders::ALL)
            .border_style(bs)
            .title(" Content  [Ctrl+O = open $EDITOR] ");
        f.render_widget(
            Paragraph::new(form.content.clone())
                .block(block)
                .wrap(Wrap { trim: false })
                .style(Style::default().fg(C_WHITE)),
            rows[3],
        );
    }

    // Help bar
    f.render_widget(
        Paragraph::new(Span::styled(
            "  Tab/Shift+Tab navigate fields   Ctrl+S save   Esc cancel",
            Style::default().fg(C_DIM),
        )),
        rows[4],
    );
}

fn input_field(f: &mut Frame, area: Rect, label: &str, value: &str, focused: bool) {
    let bs    = if focused { Style::default().fg(C_ACCENT) } else { Style::default().fg(C_DIM) };
    let block = Block::default().borders(Borders::ALL).border_style(bs).title(format!(" {} ", label));
    let text  = if focused {
        format!("{}_", value)
    } else {
        value.to_string()
    };
    f.render_widget(
        Paragraph::new(Span::styled(text, Style::default().fg(C_WHITE))).block(block),
        area,
    );
}

// ─── Confirm delete popup ─────────────────────────────────────────────────────

fn render_confirm(f: &mut Frame, area: Rect) {
    let w = 46u16;
    let h = 6u16;
    let x = (area.width.saturating_sub(w)) / 2;
    let y = (area.height.saturating_sub(h)) / 2;
    let popup = Rect::new(x, y, w, h);

    f.render_widget(Clear, popup);

    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(C_WARN))
        .title(Span::styled("  Confirm Delete  ", Style::default().fg(C_WARN)));

    f.render_widget(
        Paragraph::new("\n  Delete this entry?  [ y ] yes   [ n ] no")
            .block(block)
            .style(Style::default().fg(C_WHITE)),
        popup,
    );
}
