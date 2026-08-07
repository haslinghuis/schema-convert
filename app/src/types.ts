export interface Link {
  net: string;
  pin: string;
  side: string;
  checked: boolean;
  ok: boolean;
  gpio: boolean;
}

export interface PartHit {
  driver: string;
  marking: string;
  fitted: boolean;
}

export interface Meta {
  target: string;
  manufacturer: string;
  agreement: number;
  offset: number;
  page_description: string;
  page_count: number;
  hse_mhz: number | null;
  firmware: { rev?: string; branch?: string; date?: string };
  parts: Record<string, PartHit[]>;
  links: Link[];
  /** Functions a target normally has that this sheet did not yield. */
  absent: string[];
  /** Functions supplied by hand this run, as function -> pin. */
  placed: Record<string, string>;
  /** Candidates for each absent function, best first. */
  suggestions: Record<string, { pin: string; net: string; score: number }[]>;
}

export interface Report {
  config: string;
  warnings: string[];
  notes: string[];
  meta: Meta;
}
