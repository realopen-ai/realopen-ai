import { create } from "zustand";

export interface ViewerImage {
  src: string;
  filename: string;
  downloadSrc?: string;
  lensId?: string;
  file?: File;
  ocrText?: string | null;
  sourceUrl?: string | null;
}

interface ImageViewerState {
  images: ViewerImage[];
  selected: number | null;
  returnFocus: HTMLElement | null;
  composerImage: { file: File; conversationId: string | null } | null;
  open: (
    images: ViewerImage[],
    index: number,
    returnFocus?: HTMLElement,
  ) => void;
  close: () => void;
  select: (index: number) => void;
  attach: (file: File, conversationId: string | null) => void;
  consumeAttachment: () => void;
}

// Image files and viewer state stay in memory, including temporary-chat images.
export const useImageViewerStore = create<ImageViewerState>((set) => ({
  images: [],
  selected: null,
  returnFocus: null,
  composerImage: null,
  open: (images, selected, returnFocus) =>
    set({ images, selected, returnFocus: returnFocus ?? null }),
  close: () => set({ selected: null, images: [] }),
  select: (selected) => set({ selected }),
  attach: (file, conversationId) =>
    set({
      composerImage: { file, conversationId },
      selected: null,
      images: [],
    }),
  consumeAttachment: () => set({ composerImage: null }),
}));
