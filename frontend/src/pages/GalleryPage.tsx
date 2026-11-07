import { GalleryApp } from "@/components/gallery/GalleryApp";

// Records -> Data gallery (/records/gallery).
//
// Thin shell: view switching happens in GalleryApp via the URL query, not SubTabs, so this page
// does not use useStickyTab (stickyTab.test.ts only checks pages that use <SubTabs). Restoring
// the last view is handled by GalleryApp itself (localStorage["mast.gallery.view"], validated on read).

export default function GalleryPage() {
  return <GalleryApp />;
}
