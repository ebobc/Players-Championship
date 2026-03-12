/**
 * Fantasy Golf Participants & Picks
 *
 * Each person picks 4 golfers. Lowest combined score = 1st draft pick.
 * Golfer names use PGA Tour display names for API matching.
 */

export interface Participant {
  name: string;
  golfers: [string, string, string, string];
}

export const PARTICIPANTS: Participant[] = [
  {
    name: "Gruzz",
    golfers: ["Rory McIlroy", "Xander Schauffele", "Chris Gotterup", "Cameron Young"],
  },
  {
    name: "John N",
    golfers: ["Min Woo Lee", "Rory McIlroy", "Si Woo Kim", "Xander Schauffele"],
  },
  {
    name: "Bryce",
    golfers: ["Hideki Matsuyama", "Jake Knapp", "Rickie Fowler", "Akshay Bhatia"],
  },
  {
    name: "John C",
    golfers: ["Xander Schauffele", "Rory McIlroy", "Hideki Matsuyama", "Cameron Young"],
  },
  {
    name: "Odle",
    golfers: ["Denny McCarthy", "Rory McIlroy", "Si Woo Kim", "Tommy Fleetwood"],
  },
  {
    name: "Guggz",
    golfers: ["Min Woo Lee", "Sepp Straka", "Xander Schauffele", "Ludvig Åberg"],
  },
  {
    name: "Eric",
    golfers: ["Xander Schauffele", "Akshay Bhatia", "Rickie Fowler", "Hideki Matsuyama"],
  },
  {
    name: "CJ",
    golfers: ["Rory McIlroy", "Ludvig Åberg", "Hideki Matsuyama", "Viktor Hovland"],
  },
  {
    name: "Mason",
    golfers: ["Chris Gotterup", "Ludvig Åberg", "Tommy Fleetwood", "Xander Schauffele"],
  },
  {
    name: "FXM",
    golfers: ["Justin Rose", "Tommy Fleetwood", "Shane Lowry", "Xander Schauffele"],
  },
  {
    name: "Pyarrn",
    golfers: ["Min Woo Lee", "Si Woo Kim", "Xander Schauffele", "Sepp Straka"],
  },
  {
    name: "Darby",
    golfers: ["Justin Rose", "Xander Schauffele", "Si Woo Kim", "Shane Lowry"],
  },
];
