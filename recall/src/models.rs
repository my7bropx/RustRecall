#[derive(Debug, Clone, PartialEq)]
pub enum Category {
    Command,
    Note,
    Tool,
}

impl Category {
    pub fn as_str(&self) -> &'static str {
        match self {
            Category::Command => "command",
            Category::Note    => "note",
            Category::Tool    => "tool",
        }
    }

    pub fn from_str(s: &str) -> Self {
        match s {
            "command" => Category::Command,
            "tool"    => Category::Tool,
            _         => Category::Note,
        }
    }

    pub fn label(&self) -> &'static str {
        match self {
            Category::Command => "CMD",
            Category::Note    => "NOTE",
            Category::Tool    => "TOOL",
        }
    }

    pub fn all() -> &'static [Category] {
        &[Category::Command, Category::Note, Category::Tool]
    }

    pub fn cycle_next(&self) -> Category {
        match self {
            Category::Command => Category::Note,
            Category::Note    => Category::Tool,
            Category::Tool    => Category::Command,
        }
    }

    pub fn cycle_prev(&self) -> Category {
        match self {
            Category::Command => Category::Tool,
            Category::Note    => Category::Command,
            Category::Tool    => Category::Note,
        }
    }
}

#[derive(Debug, Clone)]
pub struct Entry {
    pub id:         i64,
    pub title:      String,
    pub content:    String,
    pub category:   Category,
    pub tags:       Vec<String>,
    pub created_at: String,
    pub updated_at: String,
}

impl Entry {
    pub fn tags_display(&self) -> String {
        self.tags.join(", ")
    }
}
