<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { invoke } from "@tauri-apps/api/core";
import { open, save } from "@tauri-apps/plugin-dialog";
import { writeTextFile } from "@tauri-apps/plugin-fs";
import ReportPanel from "./components/ReportPanel.vue";
import logo from "./assets/bf-logo.svg";
import type { Report } from "./types";

interface Environment {
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

const filename = computed(() => pdf.value?.split(/[/\\]/).pop() ?? "");
const canRun = computed(
  () => !!pdf.value && !!board.value.trim() && !!manufacturer.value.trim() && !busy.value,
);

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
      <img :src="logo" alt="Betaflight" class="h-7 w-7" />
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
          validated against firmware
          <span class="mono text-neutral-300">{{ env.firmware_rev }}</span>
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
