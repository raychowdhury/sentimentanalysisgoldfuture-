"use client";

// ResearchDashboard — polls the autoresearch API every 60 s and renders a
// timeline of recent experiment cycles, highlights the production model,
// and shows the AI-suggested next experiment in a card.
//
// Drop this file into your Next.js app (e.g. app/research/page.tsx or
// components/ResearchDashboard.tsx) and set NEXT_PUBLIC_RESEARCH_API to
// point at the FastAPI service.

import { useCallback, useEffect, useState } from "react";

type Bullets = Record<string, string>;

interface Cycle {
  cycle: number;
  timestamp: string;
  bullets: Bullets;
  raw: string;
}

interface ProductionModel {
  version: string;
  accuracy: number;
  sharpe: number;
  max_drawdown: number;
  created_at: string;
}

interface StatusPayload {
  program: {
    objective: string | null;
    next_experiment: string | null;
  };
  production_model: ProductionModel | null;
  recent_cycles: Cycle[];
}

const API_BASE =
  process.env.NEXT_PUBLIC_RESEARCH_API ?? "http://localhost:8000";
const POLL_MS = 60_000;

export default function ResearchDashboard() {
  const [data, setData] = useState<StatusPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchStatus = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/research/status`, {
        cache: "no-store",
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json: StatusPayload = await res.json();
      setData(json);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "unknown error");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchStatus();
    const id = setInterval(fetchStatus, POLL_MS);
    return () => clearInterval(id);
  }, [fetchStatus]);

  if (loading) return <div style={styles.status}>Loading research status…</div>;
  if (error) return <div style={{ ...styles.status, color: "#b91c1c" }}>Error: {error}</div>;
  if (!data) return null;

  const { program, production_model, recent_cycles } = data;

  return (
    <div style={styles.root}>
      <header style={styles.header}>
        <h1 style={styles.h1}>Gold AutoResearch</h1>
        <p style={styles.subtitle}>
          {program.objective ?? "No objective defined in program.md"}
        </p>
      </header>

      <section style={styles.grid}>
        <div style={styles.card}>
          <div style={styles.label}>Production Model</div>
          {production_model ? (
            <>
              <div style={styles.value}>{production_model.version}</div>
              <div style={styles.sub}>
                acc {production_model.accuracy.toFixed(4)} · sharpe{" "}
                {production_model.sharpe.toFixed(2)} · dd{" "}
                {production_model.max_drawdown.toFixed(2)}
              </div>
              <div style={styles.subMuted}>
                promoted {new Date(production_model.created_at).toLocaleString()}
              </div>
            </>
          ) : (
            <div style={styles.subMuted}>No model promoted yet</div>
          )}
        </div>

        <div style={styles.card}>
          <div style={styles.label}>Next Experiment (AI suggested)</div>
          {program.next_experiment ? (
            <pre style={styles.pre}>{program.next_experiment}</pre>
          ) : (
            <div style={styles.subMuted}>Waiting for meta-optimizer…</div>
          )}
        </div>
      </section>

      <section>
        <h2 style={styles.h2}>Recent Cycles</h2>
        <ol style={styles.timeline}>
          {recent_cycles.map((c) => {
            const promoted = (c.bullets["promoted"] ?? "").toLowerCase() === "yes";
            return (
              <li key={c.cycle} style={styles.timelineItem}>
                <div style={styles.dotWrap}>
                  <span
                    style={{
                      ...styles.dot,
                      background: promoted ? "#16a34a" : "#94a3b8",
                    }}
                  />
                </div>
                <div style={styles.cycleBody}>
                  <div style={styles.cycleHeader}>
                    <strong>Cycle {c.cycle}</strong>
                    <span style={styles.subMuted}>
                      {new Date(c.timestamp).toLocaleString()}
                    </span>
                    {promoted && <span style={styles.pill}>promoted</span>}
                  </div>
                  <dl style={styles.bulletGrid}>
                    {Object.entries(c.bullets).map(([k, v]) => (
                      <div key={k} style={styles.bulletRow}>
                        <dt style={styles.bulletKey}>{k}</dt>
                        <dd style={styles.bulletVal}>{v}</dd>
                      </div>
                    ))}
                  </dl>
                </div>
              </li>
            );
          })}
          {recent_cycles.length === 0 && (
            <li style={styles.subMuted}>No cycles logged yet.</li>
          )}
        </ol>
      </section>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  root: { padding: "1.5rem", fontFamily: "Inter, system-ui, sans-serif", color: "#0f172a" },
  status: { padding: "1.5rem", color: "#64748b" },
  header: { marginBottom: "1.25rem" },
  h1: { fontSize: "1.6rem", margin: 0 },
  h2: { fontSize: "1.1rem", marginTop: "1.5rem" },
  subtitle: { color: "#475569", fontSize: ".9rem", marginTop: ".25rem" },
  grid: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))",
    gap: "1rem",
    marginBottom: "1.25rem",
  },
  card: {
    background: "#fff",
    border: "1px solid #e2e8f0",
    borderRadius: 12,
    padding: "1rem 1.2rem",
  },
  label: {
    fontSize: ".6rem",
    fontWeight: 700,
    textTransform: "uppercase",
    letterSpacing: ".08em",
    color: "#94a3b8",
    marginBottom: ".5rem",
  },
  value: { fontSize: "1.3rem", fontWeight: 700 },
  sub: { fontSize: ".85rem", color: "#334155", marginTop: ".25rem" },
  subMuted: { fontSize: ".8rem", color: "#94a3b8" },
  pre: {
    background: "#f8fafc",
    border: "1px solid #e2e8f0",
    borderRadius: 6,
    padding: ".6rem .8rem",
    fontSize: ".82rem",
    whiteSpace: "pre-wrap",
    margin: 0,
  },
  timeline: { listStyle: "none", padding: 0, margin: 0 },
  timelineItem: {
    display: "grid",
    gridTemplateColumns: "24px 1fr",
    gap: ".75rem",
    padding: ".75rem 0",
    borderBottom: "1px solid #f1f5f9",
  },
  dotWrap: { display: "flex", justifyContent: "center", paddingTop: ".35rem" },
  dot: { width: 10, height: 10, borderRadius: 999, display: "inline-block" },
  cycleBody: {},
  cycleHeader: {
    display: "flex",
    alignItems: "center",
    gap: ".6rem",
    fontSize: ".9rem",
  },
  pill: {
    fontSize: ".6rem",
    fontWeight: 700,
    textTransform: "uppercase",
    letterSpacing: ".06em",
    padding: ".1rem .45rem",
    borderRadius: 999,
    background: "#dcfce7",
    color: "#15803d",
  },
  bulletGrid: { margin: ".4rem 0 0 0" },
  bulletRow: { display: "flex", gap: ".6rem", fontSize: ".82rem", padding: ".1rem 0" },
  bulletKey: {
    width: 110,
    color: "#94a3b8",
    textTransform: "capitalize",
    margin: 0,
  },
  bulletVal: { margin: 0, color: "#334155", flex: 1 },
};
