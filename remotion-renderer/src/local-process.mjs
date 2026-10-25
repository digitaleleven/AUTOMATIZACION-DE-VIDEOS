import { spawn } from "node:child_process";

// Streaming child processes: never load the MOV or unlimited diagnostics into Node.
export function runLocalProcess(command, args, {
  cwd,
  timeoutMs = 2 * 60 * 60 * 1000,
  maxStdoutBytes = 2 * 1024 * 1024,
  maxStderrBytes = 128 * 1024,
  onStdout,
} = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd,
      stdio: ["ignore", "pipe", "pipe"],
      detached: process.platform !== "win32",
    });
    let stdout = "";
    let stderr = "";
    let failure = null;
    let killTimer;
    const kill = (signal) => {
      try {
        if (process.platform !== "win32") process.kill(-child.pid, signal);
        else child.kill(signal);
      } catch (error) {
        if (error.code !== "ESRCH") child.kill(signal);
      }
    };
    const stop = (message) => {
      if (failure) return;
      failure = message;
      kill("SIGTERM");
      killTimer = setTimeout(() => kill("SIGKILL"), 2000);
      killTimer.unref();
    };
    const timer = setTimeout(() => stop(`Tiempo máximo local agotado (${timeoutMs} ms).`), timeoutMs);
    timer.unref();
    const interrupted = () => stop("Proceso local interrumpido.");
    process.once("SIGTERM", interrupted);
    process.once("SIGINT", interrupted);
    const cleanup = () => {
      clearTimeout(timer);
      clearTimeout(killTimer);
      process.removeListener("SIGTERM", interrupted);
      process.removeListener("SIGINT", interrupted);
    };
    child.stdout.on("data", (chunk) => {
      if (onStdout) onStdout(chunk.toString("utf8"));
      stdout += chunk.toString("utf8");
      if (Buffer.byteLength(stdout) > maxStdoutBytes) {
        stdout = Buffer.from(stdout).subarray(0, maxStdoutBytes).toString("utf8");
        stop("La salida del proceso superó el límite de diagnóstico.");
      }
    });
    child.stderr.on("data", (chunk) => {
      stderr = Buffer.from(stderr + chunk.toString("utf8")).subarray(-maxStderrBytes).toString("utf8");
    });
    child.once("error", (error) => {
      cleanup();
      reject(error);
    });
    child.once("close", (code, signal) => {
      cleanup();
      if (code === 0 && !failure) resolve({ stdout, stderr });
      else {
        const error = new Error(
          failure ?? `${command} exited with code ${code ?? "null"}${signal ? ` (${signal})` : ""}`,
        );
        error.stdout = stdout;
        error.stderr = stderr;
        reject(error);
      }
    });
  });
}
