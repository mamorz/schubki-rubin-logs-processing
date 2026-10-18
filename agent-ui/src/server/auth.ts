import { execFile, spawn, type ChildProcess } from "node:child_process";
import { promisify } from "node:util";
import type { AuthState } from "../shared/events.js";

const run = promisify(execFile);
const ANSI = /\x1b\[[0-9;?]*[A-Za-z]/g;

export function parseWhoami(output: string): Pick<AuthState, "loggedIn" | "method" | "account"> {
  const text = output.replace(ANSI, "");
  if (/not logged in/i.test(text)) return { loggedIn: false, method: null, account: null };
  const method = /Logged in with (.+)/i.exec(text)?.[1]?.trim() ?? null;
  const account = /Email:\s*(\S+)/i.exec(text)?.[1] ?? null;
  return { loggedIn: method !== null, method, account };
}

export function parseDeviceLogin(output: string): { url: string; code: string } | null {
  const text = output.replace(ANSI, "");
  const code = /Code:\s*([A-Z0-9-]+)/.exec(text)?.[1];
  const url = /Open this URL:\s*(https:\/\/\S+)/.exec(text)?.[1];
  return code && url ? { url, code } : null;
}

/** Wraps `kiro-cli login --use-device-flow`, `whoami` and `logout`. Credentials stay in kiro-cli's own store. */
export class KiroAuth {
  private login: ChildProcess | null = null;
  private pending: AuthState["pending"] = null;
  private error: string | null = null;

  constructor(private readonly kiroBin: string) {}

  async state(): Promise<AuthState> {
    const who = await this.whoami();
    if (who.loggedIn) this.pending = null;
    return {
      ...who,
      pending: this.pending,
      error: this.error,
      apiKeyConfigured: Boolean(process.env.KIRO_API_KEY),
    };
  }

  async version(): Promise<string | null> {
    try {
      const { stdout } = await run(this.kiroBin, ["--version"], { timeout: 10_000 });
      return stdout.trim().replace(/^kiro-cli\s*/, "") || null;
    } catch {
      return null;
    }
  }

  /** Starts device-flow login and resolves once the verification URL and code are known. */
  async startLogin(): Promise<AuthState["pending"]> {
    if (this.pending && this.login) return this.pending;
    this.error = null;
    const child = spawn(this.kiroBin, ["login", "--license", "free", "--use-device-flow"], {
      stdio: ["ignore", "pipe", "pipe"],
    });
    this.login = child;
    let output = "";
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        child.kill("SIGTERM");
        reject(new Error("kiro-cli did not print a device code"));
      }, 20_000);
      const onData = (chunk: Buffer) => {
        output = (output + chunk.toString()).slice(-8000);
        const device = this.pending ?? parseDeviceLogin(output);
        if (device && !this.pending) {
          this.pending = device;
          clearTimeout(timer);
          resolve(device);
        }
      };
      child.stdout?.on("data", onData);
      child.stderr?.on("data", onData);
      child.on("exit", (code) => {
        clearTimeout(timer);
        this.login = null;
        this.pending = null;
        if (code !== 0) {
          const tail = output.replace(ANSI, "").replace(/[^\n]*Logging in\.\.\./g, "").trim().split("\n").slice(-2).join(" ");
          this.error = `login ended (${code}): ${tail || "no output"}`;
          reject(new Error(this.error));
        } else {
          resolve(null);
        }
      });
    });
  }

  cancelLogin(): void {
    this.login?.kill("SIGTERM");
    this.login = null;
    this.pending = null;
  }

  async logout(): Promise<void> {
    this.cancelLogin();
    await run(this.kiroBin, ["logout"], { timeout: 15_000 }).catch(() => undefined);
  }

  private async whoami(): Promise<Pick<AuthState, "loggedIn" | "method" | "account">> {
    try {
      const { stdout, stderr } = await run(this.kiroBin, ["whoami"], { timeout: 10_000 });
      return parseWhoami(stdout + stderr);
    } catch (error) {
      const e = error as { stdout?: string; stderr?: string };
      return parseWhoami(`${e.stdout ?? ""}${e.stderr ?? ""}` || "not logged in");
    }
  }
}
