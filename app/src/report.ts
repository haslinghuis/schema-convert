import type { Report } from "./types";

/**
 * How the report's warnings are grouped, and how the whole report is written
 * out as Markdown.
 *
 * The grouping lives here rather than in ReportPanel.vue because two things now
 * need it: the panel that shows the report and the file that saves it. A second
 * copy of these patterns would drift from the first, and a warning that fell
 * through one but not the other would leave the saved file and the screen
 * disagreeing about what the tool refused to decide - which is the one thing
 * this report exists to state plainly.
 */
export const ACTION = [
  {
    key: "vendor",
    label: "Ask the vendor",
    hint: "Not on the schematic at all. No tool can recover these.",
    match: /orientation|current meter|ESC shunt|polarity|vendor|confirm it with/i,
  },
  {
    key: "resolve",
    label: "Needs a decision",
    hint: "The sheet is ambiguous here; the tool declined rather than guess.",
    match: /only a CS net|cannot be told|not clear enough|by hand|unknown|second IMU/i,
  },
  {
    key: "clock",
    label: "Clock and power",
    hint: "Silent if wrong: the build defaults rather than failing.",
    match: /HSE|crystal|SYSTEM_HSE_MHZ|VOLTAGE_METER/i,
  },
] as const;

export interface WarningGroup {
  key: string;
  label: string;
  hint: string;
  items: string[];
}

/**
 * Every warning, in exactly one group, with nothing dropped.
 *
 * The leftovers land in "Other" rather than being discarded, so the number of
 * warnings in the groups always equals the number the pipeline produced.
 */
export function groupWarnings(warnings: readonly string[]): WarningGroup[] {
  const rest = [...warnings];
  const out: WarningGroup[] = [];
  for (const g of ACTION) {
    const items: string[] = [];
    for (let i = rest.length - 1; i >= 0; i--) {
      if (g.match.test(rest[i])) items.unshift(rest.splice(i, 1)[0]);
    }
    if (items.length) {
      out.push({ key: g.key, label: g.label, hint: g.hint, items });
    }
  }
  if (rest.length) {
    out.push({ key: "other", label: "Other", hint: "", items: rest });
  }
  return out;
}

function basename(path: string): string {
  const m = /[^/\\]+$/.exec(path);
  return m ? m[0] : path;
}

/** A cell that cannot break the table it sits in. */
function cell(text: string): string {
  return text.replace(/\|/g, "\\|").replace(/\r?\n/g, " ");
}

export interface MarkdownOptions {
  /** The schematic this came from, for provenance. */
  pdfPath?: string | null;
  /** Local date, stamped by the caller so this stays pure. */
  date?: string;
}

/**
 * The report as a Markdown document.
 *
 * Written to be read by someone who was not sitting at the app: the numbers the
 * conversion rested on, then every warning grouped by what has to be done about
 * it, then the reasoning, then the pin map, then the file itself. Nothing is
 * summarised into a count - a reader who cannot see the warnings cannot judge
 * the config, and a report that hides them is worse than no report.
 */
export function reportMarkdown(report: Report, opts: MarkdownOptions = {}): string {
  const m = report.meta;
  const L: string[] = [];
  const agreement = Math.round((m.agreement ?? 0) * 100);
  const title = m.target ? `${m.target} conversion report` : "Conversion report";

  L.push(`# ${title}`, "");

  L.push("| | |", "|---|---|");
  const rows: [string, string | null | undefined][] = [
    ["Target", m.target],
    ["Manufacturer", m.manufacturer],
    ["Firmware agreement", `${agreement}%`],
    // Two decimals, as the terminal prints it. The raw value carries the full
    // float and reads as false precision on a quantity measured off a PDF.
    [
      "Row offset",
      typeof m.offset === "number"
        ? `${m.offset >= 0 ? "+" : ""}${m.offset.toFixed(2)}pt`
        : null,
    ],
    ["MCU symbol", m.page_description?.trim() || (m.page_count ? `1 of ${m.page_count}` : null)],
    ["HSE", m.hse_mhz ? `${m.hse_mhz} MHz` : null],
    [
      "Pin tables from",
      m.firmware?.rev
        ? `${m.firmware.rev}${m.firmware.branch ? ` (${m.firmware.branch})` : ""}` +
          `${m.firmware.date ? `, ${m.firmware.date}` : ""}`
        : null,
    ],
    ["Schematic", opts.pdfPath ? basename(opts.pdfPath) : null],
    ["Report written", opts.date ?? null],
  ];
  for (const [k, v] of rows) {
    if (v) L.push(`| ${cell(k)} | ${cell(String(v))} |`);
  }
  L.push("");

  // The branch caveat is the one piece of provenance a reader cannot recover
  // from the file, and it decides whether the target can be flown at all.
  if (m.firmware?.branch && m.firmware.branch !== "master") {
    L.push(
      `> **These pin tables come from \`${m.firmware.branch}\`, a working branch, ` +
        "not from master.** If this target relies on a pin only those tables carry, " +
        "it will not work until that change has merged.",
      "",
    );
  }

  L.push(
    "> A strong first draft, not a substitute for review. A config can build",
    "> cleanly and still not match the hardware — read the agreement score and",
    "> every warning below before shipping a target.",
    "",
  );

  const parts = Object.entries(m.parts ?? {});
  if (parts.length) {
    L.push("## Parts recognised", "", "| | marking | driver |", "|---|---|---|");
    for (const [cat, hits] of parts) {
      for (const h of hits) {
        // "not fitted" rides with the driver rather than in a column of its
        // own, which would be empty on almost every row.
        const driver = `\`${cell(h.driver)}\`${h.fitted ? "" : " — not fitted"}`;
        L.push(`| ${cell(cat)} | \`${cell(h.marking)}\` | ${driver} |`);
      }
    }
    L.push("");
  }

  const placed = Object.entries(m.placed ?? {});
  if (placed.length) {
    L.push(
      "## Supplied by hand",
      "",
      "These were not read from the sheet. Each was still checked against the",
      "firmware tables before it was emitted.",
      "",
    );
    for (const [name, pin] of placed) L.push(`- \`${cell(name)}\` = \`${cell(pin)}\``);
    L.push("");
  }

  const groups = groupWarnings(report.warnings);
  if (!report.warnings.length) {
    L.push(
      "## Warnings",
      "",
      "Nothing was left undecided. Still read the file before shipping it — a",
      "config can be self-consistent and still not match the hardware.",
      "",
    );
  } else {
    L.push(`## Warnings (${report.warnings.length})`, "");
    for (const g of groups) {
      L.push(`### ${g.label} (${g.items.length})`, "");
      if (g.hint) L.push(`*${g.hint}*`, "");
      for (const w of g.items) L.push(`- ${w}`);
      L.push("");
    }
  }

  if (m.absent?.length) {
    L.push(
      `## Not produced from this sheet (${m.absent.length})`,
      "",
      "Functions a target normally has that this schematic did not yield.",
      "",
    );
    for (const name of m.absent) {
      const sug = (m.suggestions ?? {})[name] ?? [];
      const tail = sug.length
        ? ` — candidates: ${sug.map((s) => `\`${s.pin}\` (${s.net})`).join(", ")}`
        : "";
      L.push(`- \`${cell(name)}\`${tail}`);
    }
    L.push("");
  }

  if (report.notes.length) {
    L.push(`## How it decided (${report.notes.length})`, "");
    for (const n of report.notes) L.push(`- ${n}`);
    L.push("");
  }

  const links = m.links ?? [];
  if (links.length) {
    L.push(
      `## Pin map (${links.length})`,
      "",
      "`checked` is whether the net name implied something the firmware tables",
      "could test; `ok` is whether they agreed.",
      "",
      "| net | pin | side | checked | ok |",
      "|---|---|---|---|---|",
    );
    for (const l of links) {
      const ok = !l.checked ? "—" : l.ok ? "yes" : "**no**";
      const pin = l.gpio ? `\`${cell(l.pin)}\`` : `\`${cell(l.pin)}\` (not a GPIO)`;
      L.push(
        `| \`${cell(l.net)}\` | ${pin} | ${cell(l.side)} | ` +
          `${l.checked ? "yes" : "no"} | ${ok} |`,
      );
    }
    L.push("");
  }

  if (report.config) {
    L.push("## config.h", "", "```c", report.config.replace(/\s+$/, ""), "```", "");
  }

  return L.join("\n");
}
