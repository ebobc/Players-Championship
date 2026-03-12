import { NextResponse } from "next/server";

const RAPIDAPI_BASE = "https://live-golf-data.p.rapidapi.com";

export async function GET() {
  const apiKey = process.env.RAPIDAPI_KEY;
  if (!apiKey) {
    return NextResponse.json({ error: "No RAPIDAPI_KEY" }, { status: 500 });
  }

  try {
    const res = await fetch(
      `${RAPIDAPI_BASE}/leaderboard?orgId=1&tournId=011&year=2026`,
      {
        headers: {
          "x-rapidapi-host": "live-golf-data.p.rapidapi.com",
          "x-rapidapi-key": apiKey,
        },
      }
    );
    const data = await res.json();
    const rows = data.leaderboardRows ?? data.leaderboard ?? [];
    const firstRow = rows[0];
    return NextResponse.json({
      status: res.status,
      rowCount: rows.length,
      firstRowKeys: firstRow ? Object.keys(firstRow) : [],
      firstRowSample: firstRow ?? null,
    });
  } catch (err) {
    return NextResponse.json(
      { error: String(err), cause: err instanceof Error ? String(err.cause) : "" },
      { status: 500 }
    );
  }
}
