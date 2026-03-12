import { NextResponse } from "next/server";

export async function GET() {
  return NextResponse.json({
    authRequired: !!process.env.LEADERBOARD_SECRET,
  });
}
