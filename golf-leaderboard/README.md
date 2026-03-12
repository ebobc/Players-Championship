# Fantasy Golf Draft Order Leaderboard

Live web leaderboard for your fantasy baseball draft order, based on the PGA Players Championship. Each person picks 4 golfers—lowest combined score gets 1st pick, and so on.

## Setup

### 1. Install dependencies

```bash
cd golf-leaderboard
npm install
```

### 2. Get a Slash Golf API key (free)

The Slash Golf API is accessed via RapidAPI. Per [slashgolf.dev](https://slashgolf.dev):

1. Go to [slashgolf.dev](https://slashgolf.dev) and click **Sign Up** on the Basic (free) plan
2. Or go directly to [rapidapi.com/slashgolf/api/live-golf-data/pricing](https://rapidapi.com/slashgolf/api/live-golf-data/pricing)
3. Subscribe to **Basic** (free) — 250 calls/month, includes Live Leaderboards & Scorecards
4. After subscribing, copy your **RapidAPI key** from the dashboard (under "Header Parameters" or your subscription)

### 3. Configure environment

- Copy `.env.example` to `.env.local`
- Add your API key: `RAPIDAPI_KEY=your_key_from_rapidapi`

### 4. Edit participants & picks

Edit `src/config/participants.ts` with your league's picks:

```ts
export const PARTICIPANTS: Participant[] = [
  {
    name: "Eric",
    golfers: ["Scottie Scheffler", "Rory McIlroy", "Xander Schauffele", "Viktor Hovland"],
  },
  // Add everyone...
];
```

### 5. Run locally

```bash
npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

## Deploy to Vercel (free)

1. Push this folder to GitHub
2. Go to [vercel.com](https://vercel.com) and import the repo
3. Add environment variable: `RAPIDAPI_KEY` = your RapidAPI key
4. Deploy

The leaderboard will be live at `your-project.vercel.app` and auto-refreshes every 60 seconds.

## API Notes

- **Slash Golf (RapidAPI)** free tier: 250 calls/month, includes live leaderboards
- Refresh every 15 min during tournament ≈ 128 calls for 4 days—fits in free tier
- "Current hole" (live hole-by-hole) would require scorecards endpoint; leaderboard gives round totals
