"use client";

import Workspace from "../../../components/Workspace";

export default function CustomerPage({ params }: { params: { id: string } }) {
  return <Workspace section="customers" customerId={params.id} />;
}
