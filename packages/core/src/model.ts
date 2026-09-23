// The node model (plans.md §2.1, §3.2). Structure and metadata only: prose lives in nodes/<id>.md.

export const KINDS = ["work", "decision"] as const;
export type Kind = (typeof KINDS)[number];

export const STATUSES = ["todo", "in_progress", "done", "dropped"] as const;
export type Status = (typeof STATUSES)[number];

/** A full, lowercase UUIDv7. */
export type NodeId = string;

/** Where a leaf's work landed in a code repo. Only leaves carry it (I7, checked in P03). */
export interface Delivery {
  /** Key into `code.repos` in apm.yaml. */
  repo: string;
  branch?: string;
  /** Commit SHAs, oldest first. */
  commits?: string[];
  /** Pull request URL. */
  pr?: string;
}

/** Reserved for issue-tracker sync (v2). Parsed and preserved, otherwise unused. */
export interface External {
  provider: string;
  id: string;
  synced_at?: string;
  synced_hash?: string;
}

/**
 * One node, as held in memory. `parent`, `kind`, `status` and `depends_on` are always present;
 * the serializer decides what to leave out of the file.
 */
export interface Node {
  id: NodeId;
  /** `null` = top level. */
  parent: NodeId | null;
  title: string;
  kind: Kind;
  status: Status;
  /** Sorted and de-duplicated. */
  depends_on: NodeId[];
  delivery?: Delivery;
  external?: External;
  /** Reserved for competing breakdowns (v2). */
  variant_of?: NodeId;
}
