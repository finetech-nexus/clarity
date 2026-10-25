"use client";

import Workspace from "../../components/Workspace";

export default function SectionPage({ params }: { params: { section: string } }) {
  return <Workspace section={params.section} />;
}
