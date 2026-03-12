import { Redis } from "@upstash/redis";

const CACHE_TTL = 5 * 60 * 60; // 5 hours in seconds
const GOLF_KEY = "golf-leaderboard:cache";
const HOLES_KEY = "golf-leaderboard:holes-cache";

let memoryGolf: { data: unknown; lastUpdated: string } | null = null;
let memoryHoles: { data: unknown; lastUpdated: string } | null = null;

function getRedis(): Redis | null {
  const url = process.env.KV_REST_API_URL || process.env.UPSTASH_REDIS_REST_URL;
  const token = process.env.KV_REST_API_TOKEN || process.env.UPSTASH_REDIS_REST_TOKEN;
  if (url && token) {
    return new Redis({ url, token });
  }
  return null;
}

export async function getGolfCache(): Promise<unknown> {
  const redis = getRedis();
  if (redis) {
    const cached = await redis.get<unknown>(GOLF_KEY);
    return cached;
  }
  return memoryGolf?.data ?? null;
}

export async function setGolfCache(data: unknown) {
  const redis = getRedis();
  if (redis) {
    await redis.set(GOLF_KEY, data, { ex: CACHE_TTL });
  }
  memoryGolf = { data, lastUpdated: new Date().toISOString() };
}

export async function getHolesCache(): Promise<unknown> {
  const redis = getRedis();
  if (redis) {
    const cached = await redis.get<unknown>(HOLES_KEY);
    return cached;
  }
  return memoryHoles?.data ?? null;
}

export async function setHolesCache(data: unknown) {
  const redis = getRedis();
  if (redis) {
    await redis.set(HOLES_KEY, data, { ex: CACHE_TTL });
  }
  memoryHoles = { data, lastUpdated: new Date().toISOString() };
}
