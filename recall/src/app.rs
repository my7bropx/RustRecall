use anyhow::Result;
use fuzzy_matcher::{skim::SkimMatcherV2, FuzzyMatcher};

use crate::{
    db::Database,
    models::{Category, Entry},
};

// ─── App state machine ───────────────────────────────────────────────────────

#[derive(Debug, Clone, PartialEq)]
pub enum AppState {
    Browsing,
    AddForm,
    EditForm,
    ViewFull,
    ConfirmDelete,
}

// ─── Form field focus ─────────────────────────────────────────────────────────

#[derive(Debug, Clone, PartialEq)]
pub enum FormField {
    Title,
    Category,
    Tags,
    Content,
}

impl FormField {
    pub fn next(&self) -> FormField {
        match self {
            FormField::Title    => FormField::Category,
            FormField::Category => FormField::Tags,
            FormField::Tags     => FormField::Content,
            FormField::Content  => FormField::Title,
        }
    }

    pub fn prev(&self) -> FormField {
        match self {
            FormField::Title    => FormField::Content,
            FormField::Category => FormField::Title,
            FormField::Tags     => FormField::Category,
            FormField::Content  => FormField::Tags,
        }
    }
}

// ─── Form state ───────────────────────────────────────────────────────────────

pub struct FormState {
    pub title:       String,
    pub category:    Category,
    pub tags:        String,
    pub content:     String,
    pub focused:     FormField,
    pub editing_id:  Option<i64>,
}

impl FormState {
    pub fn new() -> Self {
        FormState {
            title:      String::new(),
            category:   Category::Command,
            tags:       String::new(),
            content:    String::new(),
            focused:    FormField::Title,
            editing_id: None,
        }
    }

    pub fn from_entry(entry: &Entry) -> Self {
        FormState {
            title:      entry.title.clone(),
            category:   entry.category.clone(),
            tags:       entry.tags.join(", "),
            content:    entry.content.clone(),
            focused:    FormField::Title,
            editing_id: Some(entry.id),
        }
    }
}

// ─── Main App ─────────────────────────────────────────────────────────────────

pub struct App {
    pub db:              Database,
    pub state:           AppState,
    pub all_entries:     Vec<Entry>,
    pub filtered:        Vec<Entry>,
    pub selected:        usize,
    pub search:          String,
    pub form:            FormState,
    pub status:          Option<String>,
    pub cat_filter:      Option<Category>,
    pub view_scroll:     u16,
}

impl App {
    pub fn new(db: Database) -> Result<Self> {
        let all_entries = db.get_all_entries()?;
        let filtered    = all_entries.clone();
        Ok(App {
            db,
            state:       AppState::Browsing,
            all_entries,
            filtered,
            selected:    0,
            search:      String::new(),
            form:        FormState::new(),
            status:      None,
            cat_filter:  None,
            view_scroll: 0,
        })
    }

    pub fn reload(&mut self) -> Result<()> {
        self.all_entries = self.db.get_all_entries()?;
        self.apply_filter();
        Ok(())
    }

    pub fn apply_filter(&mut self) {
        let matcher = SkimMatcherV2::default();
        let q       = self.search.trim().to_lowercase();

        let mut filtered: Vec<Entry> = self
            .all_entries
            .iter()
            .filter(|e| {
                if let Some(ref cat) = self.cat_filter {
                    if &e.category != cat {
                        return false;
                    }
                }
                if q.is_empty() {
                    return true;
                }
                let hay = format!("{} {} {}", e.title, e.tags.join(" "), e.content).to_lowercase();
                matcher.fuzzy_match(&hay, &q).is_some()
            })
            .cloned()
            .collect();

        if !q.is_empty() {
            filtered.sort_by(|a, b| {
                let ha = format!("{} {} {}", a.title, a.tags.join(" "), a.content).to_lowercase();
                let hb = format!("{} {} {}", b.title, b.tags.join(" "), b.content).to_lowercase();
                let sa = matcher.fuzzy_match(&ha, &q).unwrap_or(0);
                let sb = matcher.fuzzy_match(&hb, &q).unwrap_or(0);
                sb.cmp(&sa)
            });
        }

        self.filtered = filtered;
        if self.selected >= self.filtered.len() {
            self.selected = self.filtered.len().saturating_sub(1);
        }
    }

    pub fn selected_entry(&self) -> Option<&Entry> {
        self.filtered.get(self.selected)
    }

    pub fn move_up(&mut self) {
        if self.selected > 0 {
            self.selected -= 1;
        }
    }

    pub fn move_down(&mut self) {
        if self.selected + 1 < self.filtered.len() {
            self.selected += 1;
        }
    }

    pub fn set_cat_filter(&mut self, cat: Option<Category>) {
        self.cat_filter = cat;
        self.selected   = 0;
        self.apply_filter();
    }

    pub fn push_search(&mut self, c: char) {
        self.search.push(c);
        self.selected = 0;
        self.apply_filter();
    }

    pub fn pop_search(&mut self) {
        self.search.pop();
        self.selected = 0;
        self.apply_filter();
    }

    pub fn clear_search(&mut self) {
        self.search.clear();
        self.selected = 0;
        self.apply_filter();
    }

    pub fn save_form(&mut self) -> Result<()> {
        let title   = self.form.title.trim().to_string();
        let content = self.form.content.clone();
        if title.is_empty() {
            self.status = Some("Title cannot be empty".to_string());
            return Ok(());
        }
        let tags: Vec<String> = self.form.tags
            .split(',')
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .collect();

        if let Some(id) = self.form.editing_id {
            self.db.update_entry(id, &title, &content, self.form.category.clone(), &tags)?;
            self.status = Some(format!("Updated: {}", title));
        } else {
            self.db.add_entry(&title, &content, self.form.category.clone(), &tags)?;
            self.status = Some(format!("Added: {}", title));
        }

        self.reload()?;
        self.state = AppState::Browsing;
        Ok(())
    }

    pub fn delete_selected(&mut self) -> Result<()> {
        if let Some(entry) = self.selected_entry() {
            let id    = entry.id;
            let title = entry.title.clone();
            self.db.delete_entry(id)?;
            self.status = Some(format!("Deleted: {}", title));
        }
        self.reload()?;
        if !self.filtered.is_empty() && self.selected >= self.filtered.len() {
            self.selected = self.filtered.len() - 1;
        }
        self.state = AppState::Browsing;
        Ok(())
    }

    pub fn yank_selected(&mut self) {
        if let Some(entry) = self.selected_entry() {
            let content = entry.content.clone();
            // Try xclip → xsel → wl-copy in order
            let tools: &[(&str, &[&str])] = &[
                ("xclip",   &["-selection", "clipboard"]),
                ("xsel",    &["--clipboard", "--input"]),
                ("wl-copy", &[]),
            ];
            let mut ok = false;
            for (tool, args) in tools {
                use std::io::Write;
                use std::process::{Command, Stdio};
                if let Ok(mut child) = Command::new(tool).args(*args).stdin(Stdio::piped()).spawn() {
                    if let Some(mut stdin) = child.stdin.take() {
                        if stdin.write_all(content.as_bytes()).is_ok() {
                            child.wait().ok();
                            self.status = Some(format!("Yanked via {}", tool));
                            ok = true;
                            break;
                        }
                    }
                }
            }
            if !ok {
                self.status = Some("Install xclip, xsel, or wl-copy for clipboard support".to_string());
            }
        }
    }
}
