<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { invoke } from "@tauri-apps/api/core";
import { open, save } from "@tauri-apps/plugin-dialog";
import { writeTextFile } from "@tauri-apps/plugin-fs";
import ReportPanel from "./components/ReportPanel.vue";
import logo from "./assets/bf-logo.svg";
import type { Report } from "./types";

interface Environment {
  bundled: boolean;
  python: string | null;
  pdftotext: string | null;
  pipeline: string | null;
  firmware_rev: string | null;
  firmware_branch: string | null;
  ready: boolean;
}

const env = ref<Environment | null>(null);
const pdf = ref<string | null>(null);
const board = ref("");
const manufacturer = ref("");
const target = ref("");
const busy = ref(false);
const error = ref<string | null>(null);
const report = ref<Report | null>(null);
const tab = ref<"report" | "config">("report");
const copied = ref(false);

// Functions the sheet did not yield, as function -> pin the operator supplies.
// Kept across runs so a value survives the re-run that applies it.
const overrides = ref<Record<string, string>>({});
const extraName = ref("");

const filename = computed(() => pdf.value?.split(/[/\\]/).pop() ?? "");
const canRun = computed(
  () => !!pdf.value && !!board.value.trim() && !!manufacturer.value.trim() && !busy.value,
);

// Everything offered a box: what the sheet did not produce, plus anything
// already supplied, plus anything typed in by hand. A function that was absent
// and has now been placed must stay listed - it is no longer in `absent`,
// because supplying it is what made it appear.
const placeable = computed(() => {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const f of [...(report.value?.meta.absent ?? []), ...Object.keys(overrides.value)]) {
    if (!seen.has(f)) {
      seen.add(f);
      out.push(f);
    }
  }
  return out;
});

const PIN = /^P[A-K]\d{1,2}$/i;
const badPin = (v: string) => !!v.trim() && !PIN.test(v.trim());

// The pipeline refuses one value at a time and names it: "--set UART3_TX=PA5:
// PA5 cannot do uart_tx3 ...". Show that against the field it is about rather
// than only in the general error box.
const rejected = computed(() => {
  const m = /--set\s+([A-Za-z0-9_]+)=/.exec(error.value ?? "");
  return m ? m[1].toUpperCase() : null;
});

function addFunction() {
  const name = extraName.value.trim().toUpperCase().replace(/_PIN$/, "");
  if (name && !(name in overrides.value)) overrides.value[name] = "";
  extraName.value = "";
}

function forget(name: string) {
  delete overrides.value[name];
}

onMounted(async () => {
  env.value = await invoke<Environment>("environment");
});

async function pick() {
  const chosen = await open({
    multiple: false,
    filters: [{ name: "Schematic PDF", extensions: ["pdf", "PDF"] }],
  });
  if (typeof chosen === "string") {
    pdf.value = chosen;
    report.value = null;
    error.value = null;
  }
}

async function run() {
  if (!canRun.value) return;
  busy.value = true;
  error.value = null;
  report.value = null;
  try {
    report.value = await invoke<Report>("convert", {
      pdf: pdf.value,
      board: board.value.trim().toUpperCase(),
      manufacturer: manufacturer.value.trim().toUpperCase(),
      target: target.value.trim() || null,
      overrides: Object.fromEntries(
        Object.entries(overrides.value).filter(([, v]) => v.trim()),
      ),
    });
    tab.value = "report";
  } catch (e) {
    error.value = String(e);
  } finally {
    busy.value = false;
  }
}

async function saveConfig() {
  if (!report.value) return;
  const path = await save({
    defaultPath: "config.h",
    filters: [{ name: "Betaflight target", extensions: ["h"] }],
  });
  if (path) await writeTextFile(path, report.value.config);
}

async function copyConfig() {
  if (!report.value) return;
  await navigator.clipboard.writeText(report.value.config);
  copied.value = true;
  setTimeout(() => (copied.value = false), 1500);
}
</script>

<template>
  <div class="flex h-full flex-col">
    <header
      class="flex items-center gap-3 border-b border-neutral-800 bg-neutral-900/60 px-5 py-3"
    >
      <!-- The wordmark is 141.7x18.2. Boxing it square fits it *inside* the
           box, so h-7 w-7 drew it 28pt wide and 3.6pt tall - height only, and
           let the width follow. -->
      <img :src="logo" alt="Betaflight" class="h-6 w-auto shrink-0" />
      <div class="flex-1">
        <h1 class="text-sm font-semibold tracking-wide text-neutral-100">
          Target Generator
        </h1>
        <p class="text-xs text-neutral-500">
          Schematic PDF to Betaflight <span class="mono">config.h</span>
        </p>
      </div>
      <div v-if="env" class="text-right text-xs">
        <div v-if="env.ready" class="text-neutral-500">
          <template v-if="env.firmware_rev">
            validated against firmware
            <span class="mono text-neutral-300">{{ env.firmware_rev }}</span>
          </template>
          <template v-else>self-contained build</template>
        </div>
        <div v-else class="text-red-400">environment incomplete</div>
      </div>
    </header>

    <!-- If a prerequisite is missing, say which one. The pipeline is Python and
         poppler; a vague "failed to start" would send the reader nowhere. -->
    <div
      v-if="env && !env.ready"
      class="border-b border-red-900/60 bg-red-950/30 px-5 py-3 text-sm text-red-200"
    >
      <div class="mb-1 font-medium">This machine is missing something:</div>
      <p class="mb-1 text-xs text-red-300/70">
        This is a source checkout, which uses the tools on your machine. An
        installed build ships its own and needs none of this.
      </p>
      <ul class="ml-4 list-disc text-red-300/90">
        <li v-if="!env.python">Python 3.12 or newer is not on PATH</li>
        <li v-if="!env.pdftotext">
          <span class="mono">pdftotext</span> is not on PATH (install poppler-utils)
        </li>
        <li v-if="!env.pipeline">The conversion pipeline could not be located</li>
        <li v-if="env.pipeline && !env.firmware_rev">
          The firmware capability data is missing or unreadable
        </li>
      </ul>
    </div>

    <main class="flex min-h-0 flex-1 gap-5 p-5">
      <!-- Inputs -->
      <section class="flex w-80 shrink-0 flex-col gap-4">
        <button
          class="group flex flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed border-neutral-700 px-4 py-8 transition hover:border-bf-500 hover:bg-neutral-900"
          @click="pick"
        >
          <span class="text-3xl text-neutral-600 group-hover:text-bf-500">⬒</span>
          <span v-if="!pdf" class="text-sm text-neutral-400">Choose a schematic</span>
          <span v-else class="mono px-2 text-center text-xs break-all text-neutral-200">
            {{ filename }}
          </span>
          <span class="text-xs text-neutral-600">stays on this machine</span>
        </button>

        <label class="flex flex-col gap-1">
          <span class="text-xs text-neutral-500">Board name</span>
          <input
            v-model="board"
            placeholder="EXAMPLEH743"
            class="mono rounded-md border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm uppercase outline-none focus:border-bf-500"
          />
        </label>

        <label class="flex flex-col gap-1">
          <span class="text-xs text-neutral-500">Manufacturer ID</span>
          <input
            v-model="manufacturer"
            placeholder="CUST"
            maxlength="4"
            class="mono rounded-md border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm uppercase outline-none focus:border-bf-500"
          />
          <span class="text-xs text-neutral-600">
            Checked against the config repo's registry
          </span>
        </label>

        <details class="text-xs text-neutral-500">
          <summary class="cursor-pointer">Override the MCU</summary>
          <input
            v-model="target"
            placeholder="STM32H743"
            class="mono mt-2 w-full rounded-md border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm uppercase outline-none focus:border-bf-500"
          />
          <p class="mt-1 leading-relaxed">
            Many sheets never name their MCU. They convert perfectly once told
            which one it is.
          </p>
        </details>

        <button
          :disabled="!canRun"
          class="rounded-md bg-bf-500 px-4 py-2 text-sm font-medium text-neutral-950 transition enabled:hover:bg-bf-400 disabled:cursor-not-allowed disabled:bg-neutral-800 disabled:text-neutral-600"
          @click="run"
        >
          {{ busy ? "Converting…" : "Generate target" }}
        </button>

        <p
          v-if="error"
          class="rounded-md border border-red-900/60 bg-red-950/30 p-3 text-sm leading-relaxed text-red-200"
        >
          {{ error }}
        </p>

        <!-- What the sheet did not give. Some boards genuinely lack these; the
             ones that do not are a page that was never supplied, or a drawing
             convention the reader cannot follow. Either way the only way
             forward is to be told, so the list that reports the gap is also
             where it gets filled in. -->
        <section
          v-if="report && placeable.length"
          class="min-h-0 flex-1 overflow-y-auto rounded-xl border border-neutral-800 p-3"
        >
          <h2 class="mb-1 text-xs font-medium text-neutral-300">Not found on this sheet</h2>
          <p class="mb-3 text-xs leading-relaxed text-neutral-600">
            Give a pin and generate again. Each one is checked against the
            firmware tables and refused if it cannot do the job.
          </p>

          <div v-for="fn in placeable" :key="fn" class="mb-2">
            <div class="flex items-center gap-2">
              <label class="mono flex-1 truncate text-xs text-neutral-400" :for="`fn-${fn}`">
                {{ fn }}
              </label>
              <input
                :id="`fn-${fn}`"
                v-model="overrides[fn]"
                placeholder="PA5"
                class="mono w-24 rounded border bg-neutral-900 px-2 py-1 text-xs uppercase outline-none"
                :class="
                  rejected === fn || badPin(overrides[fn] ?? '')
                    ? 'border-red-800 focus:border-red-600'
                    : 'border-neutral-700 focus:border-bf-500'
                "
                @keyup.enter="run"
              />
              <button
                class="px-1 text-xs text-neutral-600 hover:text-neutral-300"
                title="Remove"
                @click="forget(fn)"
              >
                ✕
              </button>
            </div>
            <p v-if="report.meta.placed[fn]" class="mono mt-0.5 text-xs text-bf-400">
              placed by hand — not read from the sheet
            </p>
          </div>

          <div class="mt-3 flex items-center gap-2 border-t border-neutral-800 pt-3">
            <input
              v-model="extraName"
              placeholder="another function, e.g. MOTOR6"
              class="mono min-w-0 flex-1 rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-xs uppercase outline-none focus:border-bf-500"
              @keyup.enter="addFunction"
            />
            <button
              class="rounded border border-neutral-700 px-2 py-1 text-xs text-neutral-300 hover:border-neutral-500"
              @click="addFunction"
            >
              Add
            </button>
          </div>
        </section>
      </section>

      <!-- Output -->
      <section class="flex min-h-0 min-w-0 flex-1 flex-col">
        <div
          v-if="!report"
          class="flex flex-1 items-center justify-center rounded-xl border border-neutral-800 text-sm text-neutral-600"
        >
          The generated target and its report appear here.
        </div>

        <template v-else>
          <div class="mb-3 flex items-center gap-2">
            <button
              v-for="t in (['report', 'config'] as const)"
              :key="t"
              class="rounded-md px-3 py-1.5 text-sm capitalize transition"
              :class="
                tab === t
                  ? 'bg-neutral-800 text-neutral-100'
                  : 'text-neutral-500 hover:text-neutral-300'
              "
              @click="tab = t"
            >
              {{ t === "config" ? "config.h" : "Report" }}
              <span
                v-if="t === 'report' && report.warnings.length"
                class="ml-1 rounded bg-bf-500/20 px-1.5 text-xs text-bf-400"
              >
                {{ report.warnings.length }}
              </span>
            </button>
            <div class="flex-1"></div>
            <button
              class="rounded-md border border-neutral-700 px-3 py-1.5 text-sm text-neutral-300 hover:border-neutral-500"
              @click="copyConfig"
            >
              {{ copied ? "Copied" : "Copy" }}
            </button>
            <button
              class="rounded-md border border-neutral-700 px-3 py-1.5 text-sm text-neutral-300 hover:border-neutral-500"
              @click="saveConfig"
            >
              Save config.h
            </button>
          </div>

          <div class="min-h-0 flex-1 overflow-auto rounded-xl bg-neutral-900/50 p-4">
            <ReportPanel v-if="tab === 'report'" :report="report" />
            <pre
              v-else
              class="mono text-xs leading-relaxed whitespace-pre text-neutral-300"
              >{{ report.config }}</pre
            >
          </div>

          <p class="mt-2 text-xs leading-relaxed text-neutral-600">
            A strong first draft, not a substitute for review. A config can build
            cleanly and still not match the hardware — check the agreement score
            and every warning before shipping a target.
          </p>
        </template>
      </section>
    </main>
  </div>
</template>
