package com.kingzcheung.xime.rime

import org.junit.Assert.assertEquals
import org.junit.Test

class CandidateIndexResolverTest {

    @Test
    fun `candidate reorder resolves by text`() {
        assertEquals(
            1,
            resolveRimeCandidateIndex(0, "target", listOf("other", "target"))
        )
    }

    @Test
    fun `missing candidate does not fall back to another page position`() {
        assertEquals(
            -1,
            resolveRimeCandidateIndex(2, "previous-page-word", listOf("current-page-word"))
        )
    }

    @Test
    fun `missing identity preserves index based selection`() {
        assertEquals(2, resolveRimeCandidateIndex(2, null, listOf("candidate")))
    }

    @Test
    fun `partial candidate preview preserves remaining pinyin`() {
        assertEquals(
            CandidateInputPrefix("ksm", "kan", "看什么"),
            resolveCandidateInputPrefix(
                "ksmkan",
                RimeCandidatePreview("", "看什么", "kan"),
                "看什么"
            )
        )
        assertEquals(
            CandidateInputPrefix("yimu", "di", "一亩"),
            resolveCandidateInputPrefix(
                "yimudi",
                RimeCandidatePreview("", "一亩", "di"),
                "一亩"
            )
        )
    }

    @Test
    fun `full candidate preview is not treated as a partial selection`() {
        assertEquals(
            null,
            resolveCandidateInputPrefix(
                "kan",
                RimeCandidatePreview("", "看", ""),
                "看"
            )
        )
    }
}
