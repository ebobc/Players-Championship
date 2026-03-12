"use client";

import { useEffect, useState } from "react";

interface GolferScore {
  name: string;
  score: number | null;
  parRelative: number | null;
  position: string;
  found: boolean;
  roundScores?: (number | null)[];
  configName?: string;
}

interface ParticipantResult {
  name: string;
  golfers: GolferScore[];
  totalScore: number | null;
  totalParRelative: number | null;
  rank: number;
}

interface ApiResponse {
  tournament: { id: number; name: string; status: string; round4Started?: boolean } | null;
  participants: ParticipantResult[];
  lastUpdated: string;
  error?: string;
  hint?: string;
}

export default function Home() {
  const [data, setData] = useState<ApiResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [golferStatus, setGolferStatus] = useState<
    Record<
      string,
      { currentHole: number | null; holesCompleted: number | null; currentRound: number | null }
    >
  >(null);
  const [holesLoading, setHolesLoading] = useState(false);

  const fetchData = async (includeRounds = false) => {
    try {
      const url = includeRounds ? "/api/golf?rounds=1" : "/api/golf";
      const res = await fetch(url);
      const json = await res.json();
      if (!res.ok) {
        setError(json.error || "Failed to load");
        if (json.hint) setError((e) => `${e}. ${json.hint}`);
      } else {
        setData((prev) => {
          if (prev && !includeRounds && json.participants) {
            return {
              ...json,
              participants: json.participants.map((newP: ParticipantResult, i: number) => {
                const prevP = prev.participants[i];
                if (!prevP?.golfers || !newP.golfers) return newP;
                return {
                  ...newP,
                  golfers: newP.golfers.map((g: GolferScore, j: number) => ({
                    ...g,
                    roundScores: prevP.golfers[j]?.roundScores ?? g.roundScores,
                    configName: g.configName ?? prevP.golfers[j]?.configName,
                  })),
                };
              }),
            };
          }
          return json;
        });
        setError(null);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Network error");
    } finally {
      setLoading(false);
    }
  };

  const fetchHoles = async () => {
    setHolesLoading(true);
    try {
      const res = await fetch("/api/golf/holes");
      const json = await res.json();
      if (res.ok) setGolferStatus(json.golferStatus ?? {});
    } catch {
      setGolferStatus(null);
    } finally {
      setHolesLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
    fetchData(true);
    fetchHoles();
    const interval = setInterval(() => fetchData(), 15 * 60 * 1000);
    const roundsInterval = setInterval(() => fetchData(true), 2 * 60 * 60 * 1000);
    return () => {
      clearInterval(interval);
      clearInterval(roundsInterval);
    };
  }, []);

  if (loading && !data) {
    return (
      <main className="min-h-screen flex items-center justify-center p-6">
        <div className="text-center">
          <div className="inline-block w-12 h-12 border-4 border-emerald-500/30 border-t-emerald-400 rounded-full animate-spin mb-4" />
          <p className="text-slate-400 font-medium">Loading leaderboard...</p>
        </div>
      </main>
    );
  }

  if (error) {
    return (
      <main className="min-h-screen flex items-center justify-center p-6">
        <div className="max-w-md w-full bg-slate-800/50 border border-slate-600 rounded-2xl p-8 text-center">
          <div className="text-amber-400 text-5xl mb-4">⚠️</div>
          <h1 className="text-xl font-bold text-white mb-2">Unable to Load Data</h1>
          <p className="text-slate-300 text-sm mb-6">{error}</p>
          <button
            onClick={() => {
              setLoading(true);
              fetchData();
            }}
            className="px-6 py-2 bg-emerald-600 hover:bg-emerald-500 text-white font-medium rounded-lg transition-colors"
          >
            Retry
          </button>
        </div>
      </main>
    );
  }

  if (!data?.participants?.length) {
    return (
      <main className="min-h-screen flex items-center justify-center p-6">
        <div className="text-slate-400">No participants configured. Edit src/config/participants.ts</div>
      </main>
    );
  }

  const formatScore = (n: number | null) => {
    if (n === null) return "—";
    const sign = n > 0 ? "+" : "";
    return `${sign}${n}`;
  };

  return (
    <main className="min-h-screen p-4 md:p-8">
      <div className="max-w-4xl mx-auto">
        {/* Header */}
        <header className="text-center mb-10">
          <h1 className="text-3xl md:text-4xl font-bold text-white mb-2 tracking-tight">
            Fantasy Draft Order
          </h1>
          <p className="text-slate-400 text-lg">
            {data.tournament?.name || "PGA Players Championship"} • Lowest combined score = 1st pick
          </p>
          <p className="text-slate-500 text-sm mt-2">
            Last updated {data.lastUpdated ? new Date(data.lastUpdated).toLocaleTimeString() : "—"} • Refreshes every 15 min (API limit)
          </p>
        </header>

        {/* Leaderboard */}
        <div className="space-y-4">
          {data.participants.map((p, idx) => (
            <div
              key={p.name}
              className="bg-slate-800/60 border border-slate-600/50 rounded-xl overflow-hidden shadow-xl hover:border-slate-500/50 transition-colors"
            >
              <div className="flex items-center justify-between p-4 md:p-5 flex-wrap gap-3">
                <div className="flex items-center gap-4">
                  <div
                    className={`w-10 h-10 rounded-full flex items-center justify-center font-bold text-lg ${
                      p.rank === 1
                        ? "bg-amber-500/20 text-amber-400 border border-amber-500/40"
                        : p.rank === 2
                        ? "bg-slate-400/20 text-slate-300 border border-slate-500/40"
                        : p.rank === 3
                        ? "bg-amber-700/30 text-amber-600 border border-amber-700/50"
                        : "bg-slate-700/50 text-slate-400 border border-slate-600"
                    }`}
                  >
                    {p.rank}
                  </div>
                  <div>
                    <h2 className="text-xl font-semibold text-white">{p.name}</h2>
                    <p className="text-slate-400 text-sm">
                      Draft pick #{p.rank} •{" "}
                      {p.totalScore !== null
                        ? `Total: ${p.totalScore} (${formatScore(p.totalParRelative)})`
                        : (() => {
                            const round4Started = data.tournament?.round4Started ?? false;
                            const hasCompletedRound = (g: GolferScore) =>
                              g.roundScores?.some((r) => r !== null) ?? false;
                            const getGolferContribution = (g: GolferScore): number => {
                              const rs = g.roundScores;
                              if (!rs || rs.length < 4) return 0;
                              const [r1, r2, r3, r4] = rs;
                              if (r1 !== null && r2 !== null) {
                                if (r3 === null && r4 === null && round4Started) {
                                  return r1 + r2 + 100 + 100;
                                }
                                return r1 + r2 + (r3 ?? 0) + (r4 ?? 0);
                              }
                              return (r1 ?? 0) + (r2 ?? 0) + (r3 ?? 0) + (r4 ?? 0);
                            };
                            const withRounds = p.golfers.filter(hasCompletedRound);
                            const partial = withRounds.reduce((s, g) => s + getGolferContribution(g), 0);
                            return withRounds.length > 0
                              ? `${partial} (${withRounds.length}/4) • In progress`
                              : "In progress";
                          })()}
                    </p>
                  </div>
                </div>
                <div className="text-right">
                  <span className="text-2xl font-bold text-emerald-400">
                    {p.totalScore !== null
                      ? p.totalScore
                      : (() => {
                          const round4Started = data.tournament?.round4Started ?? false;
                          const hasCompletedRound = (g: GolferScore) =>
                            g.roundScores?.some((r) => r !== null) ?? false;
                          const getGolferContribution = (g: GolferScore): number => {
                            const rs = g.roundScores;
                            if (!rs || rs.length < 4) return 0;
                            const [r1, r2, r3, r4] = rs;
                            if (r1 !== null && r2 !== null) {
                              if (r3 === null && r4 === null && round4Started) {
                                return r1 + r2 + 100 + 100;
                              }
                              return r1 + r2 + (r3 ?? 0) + (r4 ?? 0);
                            }
                            return (r1 ?? 0) + (r2 ?? 0) + (r3 ?? 0) + (r4 ?? 0);
                          };
                          const withRounds = p.golfers.filter(hasCompletedRound);
                          return withRounds.length > 0
                            ? `${withRounds.reduce((s, g) => s + getGolferContribution(g), 0)}`
                            : "—";
                        })()}
                  </span>
                  <p className="text-slate-500 text-xs">
                    Combined
                    {p.totalScore === null &&
                      (() => {
                        const hasCompletedRound = (g: GolferScore) =>
                          g.roundScores?.some((r) => r !== null) ?? false;
                        const withRounds = p.golfers.filter(hasCompletedRound);
                        return withRounds.length > 0 && ` (${withRounds.length}/4)`;
                      })()}
                  </p>
                </div>
              </div>

              {/* Golfer breakdown */}
              <div className="border-t border-slate-600/50 bg-slate-900/30 px-4 md:px-5 py-3">
                <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                  {p.golfers.map((g) => (
                    <div
                      key={g.name}
                      className="text-sm py-2 px-3 rounded-lg bg-slate-800/50 space-y-1"
                    >
                      <div className="flex justify-between items-center">
                        <span className={g.found ? "text-slate-300" : "text-slate-500"}>
                          {g.name}
                        </span>
                        <span className="font-mono font-medium">
                          {g.score !== null ? g.score : "—"}
                          {g.parRelative !== null && (
                            <span className="text-slate-500 ml-1">
                              ({formatScore(g.parRelative)})
                            </span>
                          )}
                        </span>
                      </div>
                      <div className="text-xs text-slate-500 space-y-0.5">
                        {g.roundScores?.some((r) => r !== null) && (
                          <div>
                            <span className="text-slate-400">
                              R1:{g.roundScores[0] ?? "—"} R2:{g.roundScores[1] ?? "—"}{" "}
                              R3:{g.roundScores[2] ?? "—"} R4:{g.roundScores[3] ?? "—"}
                            </span>
                            <span className="text-slate-600 ml-1">
                              • Total adds as rounds complete
                            </span>
                          </div>
                        )}
                        {(golferStatus?.[g.configName ?? g.name]?.holesCompleted != null ||
                          golferStatus?.[g.configName ?? g.name]?.currentRound != null) && (
                          <div className="text-emerald-500/90 font-medium">
                            {golferStatus?.[g.configName ?? g.name]?.holesCompleted != null && (
                              <span>
                                {golferStatus[g.configName ?? g.name].holesCompleted} holes
                              </span>
                            )}
                            {golferStatus?.[g.configName ?? g.name]?.currentRound != null && (
                              <span>
                                {" "}
                                • Round {golferStatus[g.configName ?? g.name].currentRound}
                              </span>
                            )}
                            {golferStatus?.[g.configName ?? g.name]?.currentHole != null && (
                              <span>
                                {" "}
                                • Hole {golferStatus[g.configName ?? g.name].currentHole}
                              </span>
                            )}
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          ))}
        </div>

        {/* Hole positions button */}
        <div className="mt-8 text-center">
          <button
            onClick={fetchHoles}
            disabled={holesLoading}
            className="px-4 py-2 text-sm bg-slate-700 hover:bg-slate-600 disabled:opacity-50 text-slate-300 rounded-lg transition-colors"
          >
            {holesLoading ? "Loading..." : "Check holes & progress"}
          </button>
          <p className="text-slate-500 text-xs mt-2">
            Holes & round info auto-load on page load • ~40 API calls per refresh
          </p>
        </div>

        {/* Footer */}
        <footer className="mt-12 text-center text-slate-500 text-sm">
          <p>Data from Slash Golf API (RapidAPI) • Refreshes every 15 min • Round scores every 2 hrs</p>
        </footer>
      </div>
    </main>
  );
}
