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
}

export interface Report {
  config: string;
  warnings: string[];
  notes: string[];
  meta: Meta;
}
