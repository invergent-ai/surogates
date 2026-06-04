// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Tests for the [S#] citation linkifier.  ``splitCitations`` is the
// pure function the writer's report markdown flows through;
// ``CitationText`` is the React wrapper that turns each ``cite``
// segment into a clickable chip resolving against the collected
// research sources.

import { describe, expect, it } from "vitest";
import { splitCitations } from "../src/components/research/citation-text";

describe("splitCitations", () => {
  it("returns one text segment when no citations are present", () => {
    expect(splitCitations("hello world")).toEqual([
      { kind: "text", value: "hello world" },
    ]);
  });

  it("returns an empty array for empty input", () => {
    expect(splitCitations("")).toEqual([]);
  });

  it("extracts a single citation", () => {
    expect(splitCitations("see [S3] for details")).toEqual([
      { kind: "text", value: "see " },
      { kind: "cite", value: "S3" },
      { kind: "text", value: " for details" },
    ]);
  });

  it("extracts comma-grouped citations as separate cite segments", () => {
    expect(splitCitations("a [S1] b [S2, S3]")).toEqual([
      { kind: "text", value: "a " },
      { kind: "cite", value: "S1" },
      { kind: "text", value: " b " },
      { kind: "cite", value: "S2" },
      { kind: "cite", value: "S3" },
    ]);
  });

  it("tolerates whitespace inside comma-grouped markers", () => {
    expect(splitCitations("[S1,  S2 ,S3]")).toEqual([
      { kind: "cite", value: "S1" },
      { kind: "cite", value: "S2" },
      { kind: "cite", value: "S3" },
    ]);
  });

  it("leaves invalid markers alone", () => {
    // ``[Sx]`` does not match the strict ``S\\d+`` pattern; the
    // splitter passes the raw text through so the writer can see
    // the malformed citation.
    expect(splitCitations("see [Sx] please")).toEqual([
      { kind: "text", value: "see [Sx] please" },
    ]);
  });

  it("does not match a bare bracket without an S-prefix", () => {
    expect(splitCitations("an array index [3] is not a citation")).toEqual([
      { kind: "text", value: "an array index [3] is not a citation" },
    ]);
  });

  it("preserves a trailing text segment after the last citation", () => {
    expect(splitCitations("intro [S1] tail")).toEqual([
      { kind: "text", value: "intro " },
      { kind: "cite", value: "S1" },
      { kind: "text", value: " tail" },
    ]);
  });

  it("preserves a leading citation when the string starts with one", () => {
    expect(splitCitations("[S1] right at the start")).toEqual([
      { kind: "cite", value: "S1" },
      { kind: "text", value: " right at the start" },
    ]);
  });
});
