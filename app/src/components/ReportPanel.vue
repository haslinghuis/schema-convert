<script setup lang="ts">
import { computed } from "vue";
import type { Report } from "../types";
import { groupWarnings } from "../report";

const props = defineProps<{ report: Report }>();

/**
 * The warnings are the product, not the exhaust.
 *
 * Every one of them is something the pipeline refused to decide - a bus it
 * could not place a device on, a crystal it would not attribute, a polarity no
 * schematic states. Collapsing them into a count would turn a config that says
 * what it does not know into one that looks finished, which is the failure this
 * whole tool exists to avoid. So they are grouped by what the reader has to do
 * about them, and none is hidden.
 *
 * The grouping itself lives in ../report.ts because the saved Markdown needs
 * the same one, and two copies would drift.
 */
const groups = computed(() => groupWarnings(props.report.warnings));

const agreement = computed(() => Math.round(props.report.meta.agreement * 100));
const parts = computed(() =>
  Object.entries(props.report.meta.parts ?? {}).map(([cat, hits]) => ({
    cat,
    text: hits
      .map((h) => `${h.marking} → ${h.driver}${h.fitted ? "" : " (not fitted)"}`)
      .join(", "),
  })),
);
</script>

<template>
  <div class="flex flex-col gap-4">
    <!-- What the conversion rested on. Agreement is the headline number: the
         share of nets that the firmware tables confirmed can do their job. -->
    <div class="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <div class="rounded-lg bg-neutral-900 p-3">
        <div class="text-xs text-neutral-500">Target</div>
        <div class="mono text-sm text-neutral-100">{{ report.meta.target }}</div>
      </div>
      <div class="rounded-lg bg-neutral-900 p-3">
        <div class="text-xs text-neutral-500">Firmware agreement</div>
        <div
          class="mono text-sm"
          :class="agreement === 100 ? 'text-emerald-400' : 'text-bf-400'"
        >
          {{ agreement }}%
        </div>
      </div>
      <div class="rounded-lg bg-neutral-900 p-3">
        <div class="text-xs text-neutral-500">Validated against</div>
        <div class="mono text-sm text-neutral-100">
          {{ report.meta.firmware?.rev ?? "?" }}
        </div>
      </div>
      <div class="rounded-lg bg-neutral-900 p-3">
        <div class="text-xs text-neutral-500">Sheet</div>
        <div class="mono text-sm text-neutral-100">
          {{ report.meta.page_description?.trim() || "single page" }}
        </div>
      </div>
    </div>

    <div v-if="parts.length" class="rounded-lg bg-neutral-900 p-3">
      <div class="mb-2 text-xs text-neutral-500">Parts recognised</div>
      <div v-for="p in parts" :key="p.cat" class="flex gap-3 text-sm">
        <span class="w-16 shrink-0 text-neutral-500">{{ p.cat }}</span>
        <span class="mono text-neutral-300">{{ p.text }}</span>
      </div>
    </div>

    <div
      v-if="!report.warnings.length"
      class="rounded-lg border border-emerald-900/60 bg-emerald-950/30 p-3 text-sm text-emerald-300"
    >
      Nothing was left undecided. Still read the file before shipping it — a
      config can be self-consistent and still not match the hardware.
    </div>

    <div v-for="g in groups" :key="g.key" class="rounded-lg bg-neutral-900 p-3">
      <div class="mb-1 flex items-baseline gap-2">
        <span class="text-sm font-medium text-bf-400">{{ g.label }}</span>
        <span class="text-xs text-neutral-500">{{ g.items.length }}</span>
      </div>
      <div v-if="g.hint" class="mb-2 text-xs text-neutral-500">{{ g.hint }}</div>
      <ul class="flex flex-col gap-2">
        <li
          v-for="(w, i) in g.items"
          :key="i"
          class="border-l-2 border-neutral-700 pl-3 text-sm leading-relaxed text-neutral-300"
        >
          {{ w }}
        </li>
      </ul>
    </div>

    <details v-if="report.notes.length" class="rounded-lg bg-neutral-900 p-3">
      <summary class="cursor-pointer text-sm text-neutral-400">
        How it decided ({{ report.notes.length }})
      </summary>
      <ul class="mt-2 flex flex-col gap-2">
        <li
          v-for="(n, i) in report.notes"
          :key="i"
          class="border-l-2 border-neutral-800 pl-3 text-sm leading-relaxed text-neutral-400"
        >
          {{ n }}
        </li>
      </ul>
    </details>
  </div>
</template>
