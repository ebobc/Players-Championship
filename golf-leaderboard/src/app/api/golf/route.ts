import { NextResponse } from "next/server";
import { PARTICIPANTS } from "@/config/participants";

const RAPIDAPI_BASE = "https://live-golf-data.p.rapidapi.com";

interface LeaderboardRow {
  firstName: string;
  lastName: string;
  position: string | number;
  total?: string | number;
  totalStrokesFromCompletedRounds?: string | number;
  rounds?: Array<{
    strokes?: number | { $numberInt?: string };
    roundId?: { $numberInt?: string };
    scoreToPar?: string;
  }>;
  [key: string]: unknown;
}

interface ScheduleEntry {
  tournId?: string;
  tournamentId?: string;
  name?: string;
  tournamentName?: string;
  tournament_name?: string;
  date?: { weekNumber: number };
  [key: string]: unknown;
}

interface GolferScore {
  name: string;
  score: number | null;
  parRelative: number | null;
  position: string;
  found: boolean;
  configName?: string;
  roundScores?: (number | null)[];
}

interface ParticipantResult {
  name: string;
  golfers: GolferScore[];
  totalScore: number | null;
  totalParRelative: number | null;
  rank: number;
}

function getRapidApiHeaders(apiKey: string) {
  return {
    "x-rapidapi-host": "live-golf-data.p.rapidapi.com",
    "x-rapidapi-key": apiKey,
  };
}

function normalizeName(name: string): string {
  return name
    .toLowerCase()
    .replace(/[àáâãäå]/g, "a")
    .replace(/[èéêë]/g, "e")
    .replace(/[ìíîï]/g, "i")
    .replace(/[òóôõö]/g, "o")
    .replace(/[ùúûü]/g, "u")
    .replace(/[ýÿ]/g, "y")
    .replace(/ñ/g, "n")
    .replace(/[^a-z0-9\s]/g, "")
    .trim();
}

function namesMatch(apiFirst: string, apiLast: string, configName: string): boolean {
  const apiFull = `${apiFirst} ${apiLast}`.trim();
  const a = normalizeName(apiFull);
  const b = normalizeName(configName);
  if (a === b) return true;
  const aParts = a.split(/\s+/).filter(Boolean);
  const bParts = b.split(/\s+/).filter(Boolean);
  if (aParts.length === 0 || bParts.length === 0) return false;
  const aLast = aParts[aParts.length - 1];
  const bLast = bParts[bParts.length - 1];
  return aLast === bLast && (aParts[0] === bParts[0] || a.startsWith(bParts[0]) || b.startsWith(aParts[0]));
}

function findGolferInScoreMap(
  golferName: string,
  scoreMap: Map<string, { score: number | null; parRelative: number | null; position: string; roundScores: (number | null)[] }>,
  rows: LeaderboardRow[]
): { name: string; score: number | null; parRelative: number | null; position: string; found: boolean; configName: string; roundScores?: (number | null)[] } | null {
  const configNorm = normalizeName(golferName);
  for (const [apiKey, entry] of scoreMap.entries()) {
    if (apiKey === configNorm) {
      const row = rows.find((r) => normalizeName(`${r.firstName} ${r.lastName}`) === apiKey);
      return {
        name: row ? `${row.firstName} ${row.lastName}` : golferName,
        score: entry.score,
        parRelative: entry.parRelative,
        position: entry.position,
        found: true,
        configName: golferName,
        roundScores: entry.roundScores,
      };
    }
    const apiParts = apiKey.split(/\s+/);
    if (apiParts.length >= 2 && namesMatch(apiParts[0], apiParts.slice(1).join(" "), golferName)) {
      const row = rows.find((r) => normalizeName(`${r.firstName} ${r.lastName}`) === apiKey);
      return {
        name: row ? `${row.firstName} ${row.lastName}` : golferName,
        score: entry.score,
        parRelative: entry.parRelative,
        position: entry.position,
        found: true,
        configName: golferName,
        roundScores: entry.roundScores,
      };
    }
  }
  return null;
}

function parseParRelative(val: string | number | null | undefined): number | null {
  if (val == null) return null;
  if (typeof val === "number") return val;
  const s = String(val).trim();
  if (s === "E" || s === "" || s === "even") return 0;
  const match = s.match(/^([+-]?\d+)/);
  if (match) return parseInt(match[1], 10);
  return null;
}

function parseStrokes(val: unknown): number | null {
  if (val == null) return null;
  if (typeof val === "number") return val;
  if (typeof val === "object" && val !== null && "$numberInt" in val) {
    const n = (val as { $numberInt?: string }).$numberInt;
    return n != null ? parseInt(n, 10) : null;
  }
  const n = parseInt(String(val), 10);
  return isNaN(n) ? null : n;
}

function getScoreFromRow(row: LeaderboardRow): {
  score: number | null;
  parRelative: number | null;
  roundScores: (number | null)[];
} {
  const score = parseStrokes(row.totalStrokesFromCompletedRounds) ?? parseStrokes(row.total) ?? parseStrokes(row.score) ?? parseStrokes(row.strokes);
  const parRelative = parseParRelative(row.total) ?? parseParRelative(row.parRelative) ?? parseParRelative(row.toPar);

  const roundScores: (number | null)[] = [null, null, null, null];
  const rounds = row.rounds;
  if (Array.isArray(rounds)) {
    for (const r of rounds) {
      const roundId = r.roundId ? (typeof r.roundId === "object" && "$numberInt" in r.roundId ? parseInt((r.roundId as { $numberInt?: string }).$numberInt ?? "", 10) : null) : null;
      const strokes = parseStrokes(r.strokes);
      if (roundId >= 1 && roundId <= 4 && strokes != null) {
        roundScores[roundId - 1] = strokes;
      }
    }
  }

  return {
    score,
    parRelative,
    roundScores,
  };
}

async function fetchSchedule(year: number, apiKey: string): Promise<ScheduleEntry[]> {
  const res = await fetch(`${RAPIDAPI_BASE}/schedule?orgId=1&year=${year}`, {
    headers: getRapidApiHeaders(apiKey),
    next: { revalidate: 3600 },
  });
  if (!res.ok) throw new Error(`Schedule failed: ${res.status}`);
  const data = await res.json();
  return data.schedule ?? data.schedules ?? [];
}

async function fetchLeaderboard(tournId: string, year: number, apiKey: string, roundId?: number) {
  const params = new URLSearchParams({
    orgId: "1",
    tournId,
    year: String(year),
  });
  if (roundId != null) params.set("roundId", String(roundId));
  const res = await fetch(`${RAPIDAPI_BASE}/leaderboard?${params}`, {
    headers: getRapidApiHeaders(apiKey),
    next: { revalidate: 60 },
  });
  if (!res.ok) {
    const body = await res.text();
    let detail = "";
    try {
      const json = JSON.parse(body);
      detail = json.message || json.error || body;
    } catch {
      detail = body || res.statusText;
    }
    throw new Error(`Leaderboard failed: ${res.status}. ${detail}`);
  }
  return res.json();
}

export async function GET(request: Request) {
  const apiKey = process.env.RAPIDAPI_KEY;

  if (!apiKey) {
    return NextResponse.json(
      {
        error: "RAPIDAPI_KEY not configured. Add it in Vercel env vars or .env.local",
        hint: "Get a free API key at rapidapi.com/slashgolf/api/live-golf-data (subscribe to free tier)",
      },
      { status: 500 }
    );
  }

  const year = parseInt(
    request.nextUrl.searchParams.get("year") || process.env.TOURNAMENT_YEAR || "2026",
    10
  );
  const tournIdParam = process.env.TOURNAMENT_ID || request.nextUrl.searchParams.get("tournId");

  try {
    let tournId = tournIdParam;

    const findPlayersFromSchedule = () => {
      const schedule = fetchSchedule(year, apiKey).then((s) =>
        s.find((t: ScheduleEntry) => {
          const name = (t.name || t.tournamentName || t.tournament_name || "").toLowerCase();
          return name.includes("players") || name.includes("the players") || name.includes("sawgrass");
        })
      );
      return schedule;
    };

    if (!tournId) {
      const players = await findPlayersFromSchedule();
      if (players) {
        tournId = String(players.tournId ?? players.tournamentId ?? "");
      } else {
        return NextResponse.json(
          {
            error: "Players Championship not found in schedule.",
            hint: "Try schedule at rapidapi.com/slashgolf/api/live-golf-data to find tournId for your year.",
          },
          { status: 404 }
        );
      }
    } else {
      const players = await findPlayersFromSchedule();
      if (players) {
        tournId = String(players.tournId ?? players.tournamentId ?? tournId);
      }
    }

    const includeRounds = request.nextUrl.searchParams.get("rounds") === "1";
    let rows: LeaderboardRow[] = [];
    const roundScoresByPlayer = new Map<string, (number | null)[]>();

    let leaderboardData;
    try {
      leaderboardData = await fetchLeaderboard(tournId, year, apiKey);
    } catch (leaderboardErr) {
      const errMsg = leaderboardErr instanceof Error ? leaderboardErr.message : "";
      if (errMsg.includes("400") || errMsg.includes("leaderboards not found")) {
        const playersFromSchedule = await findPlayersFromSchedule();
        if (playersFromSchedule) {
          const scheduleTournId = String(playersFromSchedule.tournId || playersFromSchedule.tournamentId);
          if (scheduleTournId !== tournId) {
            tournId = scheduleTournId;
            leaderboardData = await fetchLeaderboard(tournId, year, apiKey);
          } else {
            throw leaderboardErr;
          }
        } else {
          throw leaderboardErr;
        }
      } else {
        throw leaderboardErr;
      }
    }
    rows = leaderboardData.leaderboardRows ?? leaderboardData.leaderboard ?? [];

    if (includeRounds) {
      for (let r = 1; r <= 4; r++) {
        const roundData = await fetchLeaderboard(tournId, year, apiKey, r);
        const roundRows = roundData.leaderboardRows ?? roundData.leaderboard ?? [];
        for (const row of roundRows) {
          const key = normalizeName(`${row.firstName || ""} ${row.lastName || ""}`.trim());
          const score = row.total ?? row.score ?? row.round1 ?? row.r1 ?? null;
          const val = typeof score === "number" ? score : null;
          const existing = roundScoresByPlayer.get(key) ?? [null, null, null, null];
          existing[r - 1] = val;
          roundScoresByPlayer.set(key, existing);
        }
      }
    }

    const scoreMap = new Map<
      string,
      { score: number | null; parRelative: number | null; position: string; roundScores: (number | null)[] }
    >();
    for (const row of rows) {
      const key = normalizeName(`${row.firstName || ""} ${row.lastName || ""}`.trim());
      const { score, parRelative, roundScores } = getScoreFromRow(row);
      const pos = row.position != null ? String(row.position) : "-";
      const mergedRounds = includeRounds ? (roundScoresByPlayer.get(key) ?? roundScores) : roundScores;
      const hasRounds = mergedRounds.some((r) => r !== null);
      if (!scoreMap.has(key)) {
        scoreMap.set(key, {
          score,
          parRelative,
          position: pos,
          roundScores: hasRounds ? mergedRounds : roundScores,
        });
      }
    }

    const round4Started = [...scoreMap.values()].some(
      (e) => e.roundScores != null && e.roundScores[3] !== null
    );

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

    const participantResults: ParticipantResult[] = PARTICIPANTS.map((p) => {
      const golfers: GolferScore[] = p.golfers.map((golferName) => {
        const found = findGolferInScoreMap(golferName, scoreMap, rows);
        if (found) return { ...found, roundScores: found.roundScores };
        return {
          name: golferName,
          score: null,
          parRelative: null,
          position: "-",
          found: false,
          configName: golferName,
        };
      });

      const hasCompletedRound = (g: GolferScore) =>
        g.roundScores?.some((r) => r !== null) ?? false;
      const golfersWithRounds = golfers.filter(hasCompletedRound);
      const allHaveR1R2 = golfersWithRounds.length === 4 &&
        golfersWithRounds.every((g) => {
          const rs = g.roundScores;
          return rs && rs.length >= 2 && rs[0] !== null && rs[1] !== null;
        });
      const totalScore = allHaveR1R2
        ? golfersWithRounds.reduce((sum, g) => sum + getGolferContribution(g), 0)
        : null;
      const totalParRelative =
        totalScore !== null && golfersWithRounds.length === 4
          ? golfersWithRounds.reduce((sum, g) => sum + (g.parRelative ?? 0), 0)
          : null;

      return {
        name: p.name,
        golfers,
        totalScore,
        totalParRelative,
        rank: 0,
      };
    });

    participantResults.sort((a, b) => {
      if (a.totalScore !== null && b.totalScore !== null) {
        return a.totalScore - b.totalScore;
      }
      if (a.totalScore === null && b.totalScore === null) {
        const partialA = a.golfers.reduce(
          (sum, g) => sum + getGolferContribution(g),
          0
        );
        const partialB = b.golfers.reduce(
          (sum, g) => sum + getGolferContribution(g),
          0
        );
        return partialA - partialB;
      }
      return a.totalScore === null ? 1 : -1;
    });

    participantResults.forEach((p, i) => {
      p.rank = i + 1;
    });

    const tournamentName = leaderboardData.tournamentName ?? leaderboardData.name ?? "PGA Tour";

    return NextResponse.json({
      tournament: {
        id: tournId,
        name: tournamentName,
        status: leaderboardData.roundStatus ?? "Unknown",
        round4Started,
      },
      participants: participantResults,
      lastUpdated: new Date().toISOString(),
    });
  } catch (err) {
    console.error("Golf API error:", err);
    const msg = err instanceof Error ? err.message : "Failed to fetch golf data";
    const cause = err instanceof Error && err.cause ? String(err.cause) : "";
    const isNetwork =
      msg.includes("fetch failed") ||
      cause.includes("ENOTFOUND") ||
      cause.includes("ECONNREFUSED") ||
      cause.includes("ETIMEDOUT");
    const isNotFound =
      msg.includes("leaderboards not found") ||
      msg.includes("Leaderboard failed: 400");
    return NextResponse.json(
      {
        error: msg,
        hint: isNotFound
          ? "2026 leaderboard data isn't available yet. The Slash Golf API typically adds it when the tournament approaches (a few weeks before). Keep your config—it should work once the 2026 Players Championship is in the API."
          : isNetwork
          ? "Network error—check internet connection. If on VPN or corporate network, try disabling."
          : "Subscribe to free tier at rapidapi.com/slashgolf/api/live-golf-data. Verify RAPIDAPI_KEY in .env.local.",
      },
      { status: 500 }
    );
  }
}
