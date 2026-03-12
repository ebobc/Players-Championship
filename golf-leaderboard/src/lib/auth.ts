import { NextRequest, NextResponse } from "next/server";

export const AUTH_HEADER = "x-access-code";

export function requireAuth(request: NextRequest): NextResponse | null {
  const secret = process.env.LEADERBOARD_SECRET;
  if (!secret) return null;
  const code =
    request.headers.get(AUTH_HEADER) ?? request.nextUrl.searchParams.get("code");
  if (code !== secret) {
    return NextResponse.json({ error: "Access code required" }, { status: 401 });
  }
  return null;
}
