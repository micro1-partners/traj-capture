export interface TrajCaptureOptions {
  /** Agent name; second path segment under trajectories/. */
  agentName: string;
  /** Environment or deployment label; third path segment. Default "default". */
  instance?: string;
  /** Enrollment code from micro1. Exchanged for an upload SAS at enrollUrl. */
  code?: string;
  /** Default https://data.micro1.ai (or TRAJ_CAPTURE_ENROLL_URL). */
  enrollUrl?: string;
  /** Persist the credential here so restarts don't re-enroll. */
  configPath?: string;
  /** Direct SAS/container URL, or file:///dir for tests. Skips enrollment. */
  sasUrl?: string;
  /** Write events here before upload and re-ship after a restart. Off by default. */
  spoolDir?: string;
  /** Max queued events before the oldest is dropped. Default 10000. */
  maxQueue?: number;
  /** Flush on process exit (beforeExit). Default true. */
  flushOnExit?: boolean;
}

export interface TrajStats { queued: number; uploaded: number; dropped: number; failed: number }

export class TrajCapture {
  constructor(opts: TrajCaptureOptions);
  static init(opts: TrajCaptureOptions): TrajCapture;
  readonly stats: TrajStats;
  /** Event methods never throw; optional durable spooling performs local disk I/O. */
  start(runId: string, meta?: Record<string, unknown>): void;
  turn(runId: string, n: number, turn: Record<string, unknown>): void;
  end(runId: string, status?: Record<string, unknown>): void;
  feedback(runId: string, fb: Record<string, unknown>): void;
  /** Wait (bounded) for queued work and retries. False while reconciliation is pending. */
  flush(timeoutMs?: number): Promise<boolean>;
  close(timeoutMs?: number): Promise<void>;
}
