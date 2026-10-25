"use client";

import { Suspense } from "react";
import Parcours from "../../components/Parcours";

export default function ParcoursPage() {
  return (
    <Suspense fallback={<p>Chargement du parcours…</p>}>
      <Parcours />
    </Suspense>
  );
}
