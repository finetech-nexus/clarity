"use client";

import { Suspense } from "react";
import Workspace from "../../components/Workspace";

export default function SectionPage({ params }: { params: { section: string } }) {
  return (
    <Suspense>
      <Workspace section={params.section} />
    </Suspense>
  );
}
