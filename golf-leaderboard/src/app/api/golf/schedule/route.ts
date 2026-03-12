import { NextResponse } from "next/server";

const RAPIDAPI_BASE = "https://live-golf-data.p.rapidapi.com";

export async function GET(request: Request) {
  const apiKey = process.env.RAPIDAPI_KEY;
  if (!apiKey) {
    return NextResponse.json({ error: "No RAPIDAPI_KEY" }, { status: 500 });
  }

  const year = request.nextUrl.searchParams.get("year") || "2026";

  try {
    const res = await fetch(
      `${RAPIDAPI_BASE}/schedule?orgId=1&year=${year}`,
      {
        headers: {
          "x-rapidapi-host": "live-golf-data.p.rapidapi.com",
          "x-rapidapi-key": apiKey,
        },
      }
    );
    const data = await res.json();
    const schedule = data.schedule ?? data.schedules ?? [];
    const players = schedule.find(
      (t: { name?: string; tournamentName?: string; tournament_name?: string }) => {
        const name = (t.name || t.tournamentName || t.tournament_name || "").toLowerCase();
        return name.includes("players") || name.includes("the players") || name.includes("sawgrass");
      }
    );
    return NextResponse.json({
      year,
      totalEvents: schedule.length,
      playersChampionship: players ?? null,
      sampleEvent: schedule[0] ?? null,
    });
  } catch (err) {
    return NextResponse.json(
      { error: String(err) },
      { status: 500 }
    );
  }
}
