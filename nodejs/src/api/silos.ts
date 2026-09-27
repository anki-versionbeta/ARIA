import { api } from "./http";
import type { Silo } from "./types";

export function fetchSilos() {
  return api.get<Silo[]>("/silos");
}
