import { NextRequest, NextResponse } from "next/server";

export const AUTH_HEADER = "x-access-code";

export function hasValidAuth(request: NextRequest): boolean {
  const secret = process.env.LEADERBOARD_SECRET;
  if (!secret) return true;
  const code =
    request.headers.get(AUTH_HEADER) ?? request.nextUrl.searchParams.get("code");
  return code === secret;
}

export function requireAuth(request: NextRequest): NextResponse | null {
  if (hasValidAuth(request)) return null;
  const secret = process.env.LEADERBOARD_SECRET;
  if (!secret) return null;
  return NextResponse.json({ error: "Access code required" }, { status: 401 });
}
