import { describe, expect, it } from "vitest";

import {
  splitComposerFiles,
  type ComposerFilePart,
} from "../src/lib/split-composer-files";

function imageFile(name = "shot.png"): File {
  return new File([new Uint8Array([1, 2, 3])], name, { type: "image/png" });
}

function docFile(name = "report.pdf"): File {
  return new File([new Uint8Array([4, 5, 6])], name, {
    type: "application/pdf",
  });
}

describe("splitComposerFiles", () => {
  it("routes an image into BOTH the inline-vision list and the upload list", () => {
    const file = imageFile();
    const part: ComposerFilePart = {
      mediaType: "image/png",
      url: "data:image/png;base64,AAAA",
      filename: "shot.png",
      file,
    };

    const { images, pending } = splitComposerFiles([part]);

    expect(images).toEqual([
      { data: "data:image/png;base64,AAAA", mimeType: "image/png" },
    ]);
    expect(pending).toHaveLength(1);
    expect(pending[0]).toMatchObject({
      file,
      filename: "shot.png",
      mimeType: "image/png",
      size: file.size,
    });
  });

  it("routes a non-image file into the upload list only", () => {
    const file = docFile();
    const part: ComposerFilePart = {
      mediaType: "application/pdf",
      filename: "report.pdf",
      file,
    };

    const { images, pending } = splitComposerFiles([part]);

    expect(images).toEqual([]);
    expect(pending).toEqual([
      {
        file,
        filename: "report.pdf",
        mimeType: "application/pdf",
        size: file.size,
      },
    ]);
  });

  it("surfaces an image inline but does not queue an upload when it has no backing File", () => {
    const part: ComposerFilePart = {
      mediaType: "image/jpeg",
      url: "data:image/jpeg;base64,BBBB",
      filename: "remote.jpg",
    };

    const { images, pending } = splitComposerFiles([part]);

    expect(images).toEqual([
      { data: "data:image/jpeg;base64,BBBB", mimeType: "image/jpeg" },
    ]);
    expect(pending).toEqual([]);
  });

  it("keeps a mixed batch separated, with the image in both buckets", () => {
    const img = imageFile();
    const doc = docFile();
    const parts: ComposerFilePart[] = [
      {
        mediaType: "image/png",
        url: "data:image/png;base64,CCCC",
        filename: "shot.png",
        file: img,
      },
      { mediaType: "application/pdf", filename: "report.pdf", file: doc },
    ];

    const { images, pending } = splitComposerFiles(parts);

    expect(images).toHaveLength(1);
    expect(pending).toHaveLength(2);
    expect(pending.map((p) => p.filename)).toEqual(["shot.png", "report.pdf"]);
  });

  it("falls back to the File name and type when filename/mediaType are absent", () => {
    const file = docFile("notes.txt");
    const part: ComposerFilePart = { file };

    const { pending } = splitComposerFiles([part]);

    expect(pending[0]).toMatchObject({
      filename: "notes.txt",
      mimeType: "application/pdf",
    });
  });
});
