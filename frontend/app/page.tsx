"use client";

import { Suspense } from "react";
import Workspace from "../components/Workspace";

export default function Page() {
  return (
    <Suspense>
      <Workspace section="overview" />
    </Suspense>
  );
}
