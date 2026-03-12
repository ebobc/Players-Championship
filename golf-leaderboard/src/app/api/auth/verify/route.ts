import { NextRequest, NextResponse } from "next/server";
import { AUTH_HEADER } from "@/lib/auth";

export async function POST(request: NextRequest) {
  const secret = process.env.LEADERBOARD_SECRET;
  if (!secret) {
    return NextResponse.json(
      { error: "Access control not configured" },
      { status: 500 }
    );
  }

  let code = request.headers.get(AUTH_HEADER);
  if (!code) {
    try {
      const body = await request.json();
      code = body?.code ?? body?.password ?? null;
    } catch {
      /* ignore */
    }
  }
  if (code === secret) {
    return NextResponse.json({ valid: true });
  }
  return NextResponse.json({ error: "Invalid access code" }, { status: 401 });
}
