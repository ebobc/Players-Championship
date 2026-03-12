import { NextRequest, NextResponse } from "next/server";
import { PARTICIPANTS } from "@/config/participants";

const RAPIDAPI_BASE = "https://live-golf-data.p.rapidapi.com";

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

export async function GET(request: NextRequest) {
  const apiKey = process.env.RAPIDAPI_KEY;
  if (!apiKey) {
    return NextResponse.json({ error: "RAPIDAPI_KEY not configured" }, { status: 500 });
  }

  const year = parseInt(
    request.nextUrl.searchParams.get("year") || process.env.TOURNAMENT_YEAR || "2026",
    10
  );
  const tournIdParam = process.env.TOURNAMENT_ID || request.nextUrl.searchParams.get("tournId");

  try {
    let tournId = tournIdParam;
    if (!tournId) {
      const scheduleRes = await fetch(`${RAPIDAPI_BASE}/schedule?orgId=1&year=${year}`, {
        headers: getRapidApiHeaders(apiKey),
      });
      if (!scheduleRes.ok) throw new Error("Schedule failed");
      const scheduleData = await scheduleRes.json();
      const schedule = scheduleData.schedule ?? [];
      const players = schedule.find(
        (t: { name?: string; tournamentName?: string }) =>
          (t.name || t.tournamentName || "").toLowerCase().includes("players")
      );
      if (!players) throw new Error("Players Championship not found");
      tournId = String(players.tournId);
    }

    const tournamentRes = await fetch(
      `${RAPIDAPI_BASE}/tournament?orgId=1&tournId=${encodeURIComponent(tournId)}&year=${year}`,
      { headers: getRapidApiHeaders(apiKey) }
    );
    if (!tournamentRes.ok) throw new Error("Tournament failed");
    const tournamentData = await tournamentRes.json();
    const players = tournamentData.players ?? [];
    const golferStatus: Record<
      string,
      { currentHole: number | null; holesCompleted: number | null; currentRound: number | null }
    > = {};

    for (const participant of PARTICIPANTS) {
      for (const golferName of participant.golfers) {
        let playerId: string | null = null;
        for (const p of players) {
          if (namesMatch(p.firstName || "", p.lastName || "", golferName)) {
            playerId = p.playerId ? String(p.playerId) : null;
            break;
          }
        }
        if (!playerId) {
          golferStatus[golferName] = { currentHole: null, holesCompleted: null, currentRound: null };
          continue;
        }

        const scorecardRes = await fetch(
          `${RAPIDAPI_BASE}/scorecards?orgId=1&tournId=${tournId}&year=${year}&playerId=${playerId}`,
          { headers: getRapidApiHeaders(apiKey) }
        );
        if (!scorecardRes.ok) {
          golferStatus[golferName] = { currentHole: null, holesCompleted: null, currentRound: null };
          continue;
        }

        const scorecardData = await scorecardRes.json();
        const cards = Array.isArray(scorecardData) ? scorecardData : scorecardData.scorecards ?? scorecardData.data ?? [];
        let currentHole: number | null = null;
        let holesCompleted = 0;
        for (const card of cards) {
          const holes = card.holes ?? card ?? [];
          const holeNumbers = holes
            .map((h: { hole_number?: number; holeNumber?: number }) => h.hole_number ?? h.holeNumber)
            .filter((n: number) => typeof n === "number");
          for (const h of holes) {
            const hasScore = h.score != null || h.strokes != null;
            if (hasScore) holesCompleted++;
          }
          const maxHole = holeNumbers.length > 0 ? Math.max(...holeNumbers) : 0;
          if (maxHole > 0) {
            currentHole = maxHole >= 18 ? 18 : maxHole + 1;
          }
        }
        const currentRound =
          holesCompleted > 0 ? Math.ceil(holesCompleted / 18) : null;

        golferStatus[golferName] = {
          currentHole,
          holesCompleted: holesCompleted > 0 ? holesCompleted : null,
          currentRound,
        };
      }
    }

    return NextResponse.json({
      golferStatus,
      lastUpdated: new Date().toISOString(),
    });
  } catch (err) {
    console.error("Holes API error:", err);
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "Failed to fetch hole data" },
      { status: 500 }
    );
  }
}
