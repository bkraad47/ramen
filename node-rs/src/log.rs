//! One JSON line per event on stdout, mirrored to `RAMEN_LOG_FILE` when set. Never log key or secret values.
use serde_json::{Map, Value};
use std::io::Write;
use std::path::PathBuf;
use std::sync::{Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

static FILE: OnceLock<Mutex<Option<PathBuf>>> = OnceLock::new();

pub fn set_file(path: Option<PathBuf>) {
    if let Some(p) = &path
        && let Some(dir) = p.parent()
    {
        let _ = std::fs::create_dir_all(dir);
    }
    *FILE
        .get_or_init(|| Mutex::new(None))
        .lock()
        .unwrap_or_else(|e| e.into_inner()) = path;
}

fn mirror(line: &str) {
    if let Some(m) = FILE.get()
        && let Some(p) = m.lock().unwrap_or_else(|e| e.into_inner()).as_ref()
        && let Ok(mut f) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(p)
    {
        let _ = writeln!(f, "{line}");
    }
}

pub fn now_iso() -> String {
    let s = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let (days, rem) = (s / 86400, s % 86400);
    let (y, m, d) = civil(days as i64);
    format!(
        "{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z",
        rem / 3600,
        rem % 3600 / 60,
        rem % 60
    )
}

fn civil(days: i64) -> (i64, i64, i64) {
    let z = days + 719468;
    let era = z.div_euclid(146097);
    let doe = z.rem_euclid(146097);
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (yoe + era * 400 + i64::from(m <= 2), m, d)
}

pub fn emit(level: &str, msg: &str, fields: Value) {
    let mut m = Map::new();
    m.insert("ts".into(), now_iso().into());
    m.insert("level".into(), level.into());
    m.insert("msg".into(), msg.into());
    if let Value::Object(f) = fields {
        m.extend(f);
    }
    let line = Value::Object(m).to_string();
    println!("{line}");
    mirror(&line);
}

/// Pass-through for sidecar stderr lines (already JSON).
pub fn raw(line: &str) {
    eprintln!("{line}");
    mirror(line);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn iso_shape() {
        let t = now_iso();
        assert_eq!(t.len(), 20);
        assert!(t.starts_with("20") && t.ends_with('Z'));
        assert_eq!(civil(0), (1970, 1, 1));
        assert_eq!(civil(20723), (2026, 9, 27));
    }

    #[test]
    fn mirrors_to_file() {
        let p = std::env::temp_dir().join(format!("ramen-log-{}/sub/w.log", std::process::id()));
        set_file(Some(p.clone()));
        emit("info", "hello", serde_json::json!({"k": 1}));
        raw("{\"sidecar\":true}");
        set_file(None);
        emit("info", "not mirrored", Value::Null);
        let text = std::fs::read_to_string(&p).unwrap();
        // the log file is process-global and other tests emit concurrently, so count only this test's own lines
        // (an exact line count flaked in CI on 0.5.3 and once on a laptop)
        let mine = text
            .lines()
            .filter(|l| l.contains("\"msg\":\"hello\"") || l.contains("sidecar"))
            .count();
        assert_eq!(mine, 2, "{text}");
        assert!(!text.contains("not mirrored"));
        let _ = std::fs::remove_dir_all(p.parent().unwrap().parent().unwrap());
    }
}
