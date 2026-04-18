use anyhow::Result;
use rusqlite::{params, Connection};
use std::path::Path;

use crate::models::{Category, Entry};

pub struct Database {
    conn: Connection,
}

impl Database {
    pub fn new(path: &Path) -> Result<Self> {
        let conn = Connection::open(path)?;
        let db = Database { conn };
        db.init()?;
        Ok(db)
    }

    fn init(&self) -> Result<()> {
        self.conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS entries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT    NOT NULL,
                content     TEXT    NOT NULL DEFAULT '',
                category    TEXT    NOT NULL DEFAULT 'note',
                tags        TEXT    NOT NULL DEFAULT '',
                created_at  TEXT    NOT NULL,
                updated_at  TEXT    NOT NULL
            );",
        )?;
        Ok(())
    }

    pub fn add_entry(
        &self,
        title:    &str,
        content:  &str,
        category: Category,
        tags:     &[String],
    ) -> Result<i64> {
        let now      = now_str();
        let tags_str = tags.join(",");
        self.conn.execute(
            "INSERT INTO entries (title, content, category, tags, created_at, updated_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![title, content, category.as_str(), tags_str, now, now],
        )?;
        Ok(self.conn.last_insert_rowid())
    }

    pub fn update_entry(
        &self,
        id:       i64,
        title:    &str,
        content:  &str,
        category: Category,
        tags:     &[String],
    ) -> Result<()> {
        let now      = now_str();
        let tags_str = tags.join(",");
        self.conn.execute(
            "UPDATE entries
             SET title=?1, content=?2, category=?3, tags=?4, updated_at=?5
             WHERE id=?6",
            params![title, content, category.as_str(), tags_str, now, id],
        )?;
        Ok(())
    }

    pub fn delete_entry(&self, id: i64) -> Result<()> {
        self.conn.execute("DELETE FROM entries WHERE id=?1", params![id])?;
        Ok(())
    }

    pub fn get_all_entries(&self) -> Result<Vec<Entry>> {
        let mut stmt = self.conn.prepare(
            "SELECT id, title, content, category, tags, created_at, updated_at
             FROM entries
             ORDER BY updated_at DESC",
        )?;
        let entries = stmt
            .query_map([], |row| {
                let tags_str: String = row.get(4)?;
                let tags = parse_tags(&tags_str);
                Ok(Entry {
                    id:         row.get(0)?,
                    title:      row.get(1)?,
                    content:    row.get(2)?,
                    category:   Category::from_str(&row.get::<_, String>(3)?),
                    tags,
                    created_at: row.get(5)?,
                    updated_at: row.get(6)?,
                })
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(entries)
    }

    pub fn search_plain(&self, query: &str) -> Result<Vec<Entry>> {
        let pattern = format!("%{}%", query);
        let mut stmt = self.conn.prepare(
            "SELECT id, title, content, category, tags, created_at, updated_at
             FROM entries
             WHERE title LIKE ?1 OR content LIKE ?1 OR tags LIKE ?1
             ORDER BY updated_at DESC",
        )?;
        let entries = stmt
            .query_map(params![pattern], |row| {
                let tags_str: String = row.get(4)?;
                let tags = parse_tags(&tags_str);
                Ok(Entry {
                    id:         row.get(0)?,
                    title:      row.get(1)?,
                    content:    row.get(2)?,
                    category:   Category::from_str(&row.get::<_, String>(3)?),
                    tags,
                    created_at: row.get(5)?,
                    updated_at: row.get(6)?,
                })
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(entries)
    }
}

fn now_str() -> String {
    chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string()
}

fn parse_tags(s: &str) -> Vec<String> {
    if s.is_empty() {
        vec![]
    } else {
        s.split(',').map(|t| t.trim().to_string()).filter(|t| !t.is_empty()).collect()
    }
}
