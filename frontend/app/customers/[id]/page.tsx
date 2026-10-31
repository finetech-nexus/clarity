"use client";

import { Suspense } from "react";
import Workspace from "../../../components/Workspace";

export default function CustomerPage({ params }: { params: { id: string } }) {
  return (
    <Suspense>
      <Workspace section="customers" customerId={params.id} />
    </Suspense>
  );
}
