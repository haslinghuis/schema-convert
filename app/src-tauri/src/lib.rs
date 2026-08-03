// This file is part of schema-convert.
//
// Copyright (C) 2026 Mark Haslinghuis
//
// schema-convert is free software. You can redistribute this software
// and/or modify this software under the terms of the GNU General Public
// License as published by the Free Software Foundation, either version 3
// of the License, or (at your option) any later version.
//
// schema-convert is distributed in the hope that it will be useful, but
// WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
// General Public License for more details.
//
// You should have received a copy of the GNU General Public License
// along with this software. If not, see <https://www.gnu.org/licenses/>.

//! The bridge to the conversion pipeline.
//!
//! The pipeline stays in Python and is called, not reimplemented. Its
//! extraction is the whole product and is validated against a 168-schematic
//! corpus and four hand-written configs; a second implementation would be a
//! second thing to keep correct, on a different PDF engine, with none of that
//! evidence behind it.
//!
//! Nothing is uploaded anywhere. The schematic is a confidential description of
//! unreleased hardware, so it is read from the user's disk by a local process
//! and never leaves the machine - which is why this is a desktop app rather
//! than a web page.

mod commands {
    use std::path::{Path, PathBuf};
    use std::process::Command;

    use serde::Serialize;

    /// What the UI needs to know before it lets anyone convert anything.
    #[derive(Serialize)]
    pub struct Environment {
        pub python: Option<String>,
        pub pdftotext: Option<String>,
        pub pipeline: Option<String>,
        pub firmware_rev: Option<String>,
        pub firmware_branch: Option<String>,
        pub ready: bool,
    }

    fn first_line(out: Vec<u8>) -> String {
        String::from_utf8_lossy(&out)
            .lines()
            .next()
            .unwrap_or("")
            .trim()
            .to_string()
    }

    fn probe(program: &str, args: &[&str]) -> Option<String> {
        let out = Command::new(program).args(args).output().ok()?;
        if !out.status.success() {
            return None;
        }
        let text = first_line(out.stdout.clone());
        Some(if text.is_empty() { first_line(out.stderr) } else { text })
    }

    /// Where the Python pipeline lives.
    ///
    /// Two layouts, and both are real: run from a source checkout during
    /// development, or from the bundled resources of an installed app.
    fn pipeline_root(app: &tauri::AppHandle) -> Option<PathBuf> {
        use tauri::Manager;
        let has_pipeline = |p: &PathBuf| p.join("genconfig.py").is_file();

        if let Ok(dir) = app.path().resource_dir() {
            let bundled = dir.join("mcu-parser");
            if has_pipeline(&bundled) {
                return Some(bundled);
            }
        }
        // In development the working directory is src-tauri/, so the checkout
        // root is two levels up rather than one - walk instead of counting,
        // because `tauri dev` and `cargo run` start from different places.
        let mut dir = std::env::current_dir().ok()?;
        loop {
            let candidate = dir.join("mcu-parser");
            if has_pipeline(&candidate) {
                return Some(candidate);
            }
            if !dir.pop() {
                return None;
            }
        }
    }

    fn python() -> &'static str {
        // python3 everywhere the pipeline is supported; py -3 on Windows installs
        // that lack the alias is a documented fallback rather than a guess here.
        if cfg!(windows) {
            "python"
        } else {
            "python3"
        }
    }

    #[tauri::command]
    pub fn environment(app: tauri::AppHandle) -> Environment {
        let root = pipeline_root(&app);
        let mut env = Environment {
            python: probe(python(), &["--version"]),
            pdftotext: probe("pdftotext", &["-v"]),
            pipeline: root.as_ref().map(|p| p.display().to_string()),
            firmware_rev: None,
            firmware_branch: None,
            ready: false,
        };
        if let Some(root) = &root {
            // The committed capability snapshot. Every emitted pin is validated
            // against it, so the UI shows which firmware revision that was.
            if let Ok(text) = std::fs::read_to_string(root.join("data/firmware.json")) {
                if let Ok(v) = serde_json::from_str::<serde_json::Value>(&text) {
                    env.firmware_rev = v["firmware"]["rev"].as_str().map(str::to_string);
                    env.firmware_branch = v["firmware"]["branch"].as_str().map(str::to_string);
                }
            }
        }
        env.ready = env.python.is_some()
            && env.pdftotext.is_some()
            && env.pipeline.is_some()
            && env.firmware_rev.is_some();
        env
    }

    /// Run one conversion and hand back the pipeline's own JSON report verbatim.
    ///
    /// The report is not reshaped on the way through. Everything the tool refuses
    /// to decide - an unresolved SPI bus, a crystal it will not attribute, a
    /// polarity no schematic states - is carried as a warning, and dropping any of
    /// those on the floor would turn a config that says what it does not know into
    /// one that looks finished.
    #[tauri::command]
    pub fn convert(
        app: tauri::AppHandle,
        pdf: String,
        board: String,
        manufacturer: String,
        target: Option<String>,
        page: Option<u32>,
        hse_mhz: Option<u32>,
    ) -> Result<serde_json::Value, String> {
        let root = pipeline_root(&app).ok_or_else(|| {
            "Could not find the conversion pipeline (mcu-parser/genconfig.py)".to_string()
        })?;
        if !Path::new(&pdf).is_file() {
            return Err(format!("No such file: {pdf}"));
        }

        let mut cmd = Command::new(python());
        cmd.arg(root.join("genconfig.py"))
            .arg(&pdf)
            .arg("--board")
            .arg(&board)
            .arg("--manufacturer")
            .arg(&manufacturer)
            .arg("--json");
        if let Some(t) = target.filter(|s| !s.is_empty()) {
            cmd.arg("--target").arg(t);
        }
        if let Some(p) = page {
            cmd.arg("--page").arg(p.to_string());
        }
        if let Some(h) = hse_mhz {
            cmd.arg("--hse-mhz").arg(h.to_string());
        }

        let out = cmd
            .output()
            .map_err(|e| format!("Could not run {}: {e}", python()))?;
        if !out.status.success() {
            // The pipeline's refusals are written for a person and say what to do
            // next - which sheet to pass, which target, what to ask the vendor. Pass
            // the last line through rather than inventing a message for it.
            let err = String::from_utf8_lossy(&out.stderr);
            let last = err.trim().lines().last().unwrap_or("").trim();
            return Err(if last.is_empty() {
                format!("Conversion failed ({})", out.status)
            } else {
                last.to_string()
            });
        }
        serde_json::from_slice(&out.stdout)
            .map_err(|e| format!("Could not read the pipeline's report: {e}"))
    }

}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_fs::init())
        .invoke_handler(tauri::generate_handler![commands::environment, commands::convert])
        .run(tauri::generate_context!())
        .expect("error while running the application");
}
