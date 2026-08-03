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
        /// True when the app is running from its own frozen pipeline rather
        /// than a checkout, i.e. when nothing needs to be installed.
        pub bundled: bool,
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

    /// What the app ships so a vendor installs nothing: the whole converter
    /// frozen into one executable, and poppler's extractor with its libraries.
    ///
    /// Both are optional. A source checkout has neither and falls back to the
    /// system Python and whatever poppler is on PATH, which is what development
    /// wants; an installed bundle has both and touches neither.
    pub struct Bundled {
        pub pipeline: Option<PathBuf>,
        pub pdftotext: Option<PathBuf>,
        pub poppler_libs: Option<PathBuf>,
    }

    fn exe_name(stem: &str) -> String {
        if cfg!(windows) { format!("{stem}.exe") } else { stem.to_string() }
    }

    fn bundled(app: &tauri::AppHandle) -> Bundled {
        use tauri::Manager;
        let root = app.path().resource_dir().ok();
        let at = |rel: &str| -> Option<PathBuf> {
            let p = root.as_ref()?.join(rel);
            p.exists().then_some(p)
        };
        Bundled {
            pipeline: at(&format!("pipeline/{}", exe_name("schema-convert"))),
            pdftotext: at(&format!("poppler/{}", exe_name("pdftotext"))),
            poppler_libs: at("poppler/lib"),
        }
    }

    /// Point the converter at the poppler we shipped, and at the libraries it
    /// needs. The libraries are found this way rather than by rewriting the
    /// binary's rpath, which would put patchelf in the build for no gain.
    fn use_bundled_poppler(cmd: &mut Command, b: &Bundled) {
        if let Some(pdftotext) = &b.pdftotext {
            cmd.env("SCHEMA_CONVERT_PDFTOTEXT", pdftotext);
        }
        if let Some(libs) = &b.poppler_libs {
            let joined = match std::env::var_os(LIB_PATH_VAR) {
                Some(existing) => {
                    let mut v = vec![libs.clone()];
                    v.extend(std::env::split_paths(&existing));
                    std::env::join_paths(v).ok()
                }
                None => Some(libs.clone().into_os_string()),
            };
            if let Some(joined) = joined {
                cmd.env(LIB_PATH_VAR, joined);
            }
        }
    }

    #[cfg(target_os = "macos")]
    const LIB_PATH_VAR: &str = "DYLD_LIBRARY_PATH";
    #[cfg(not(target_os = "macos"))]
    const LIB_PATH_VAR: &str = "LD_LIBRARY_PATH";

    #[tauri::command]
    pub fn environment(app: tauri::AppHandle) -> Environment {
        let b = bundled(&app);
        let root = pipeline_root(&app);
        // A bundled build needs nothing installed, so report what it will
        // actually use rather than what happens to be on the machine.
        let mut env = Environment {
            bundled: b.pipeline.is_some(),
            python: match &b.pipeline {
                Some(_) => Some("bundled".into()),
                None => probe(python(), &["--version"]),
            },
            pdftotext: match &b.pdftotext {
                Some(_) => Some("bundled".into()),
                None => probe("pdftotext", &["-v"]),
            },
            pipeline: b
                .pipeline
                .as_ref()
                .map(|p| p.display().to_string())
                .or_else(|| root.as_ref().map(|p| p.display().to_string())),
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
        // The frozen build carries its capability data inside the executable,
        // so there is no firmware.json beside it to read - which is a fact
        // about where the data lives, not a missing prerequisite.
        env.ready = env.python.is_some()
            && env.pdftotext.is_some()
            && env.pipeline.is_some()
            && (env.firmware_rev.is_some() || env.bundled);
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
        if !Path::new(&pdf).is_file() {
            return Err(format!("No such file: {pdf}"));
        }
        let b = bundled(&app);

        // The frozen pipeline is one executable and takes the arguments
        // directly; a checkout needs an interpreter in front of the script.
        let mut cmd = match &b.pipeline {
            Some(exe) => Command::new(exe),
            None => {
                let root = pipeline_root(&app).ok_or_else(|| {
                    "Could not find the conversion pipeline \
                     (mcu-parser/genconfig.py)"
                        .to_string()
                })?;
                let mut c = Command::new(python());
                c.arg(root.join("genconfig.py"));
                c
            }
        };
        use_bundled_poppler(&mut cmd, &b);
        cmd.arg(&pdf)
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
            .map_err(|e| format!("Could not start the converter: {e}"))?;
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
