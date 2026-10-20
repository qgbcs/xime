package com.kingzcheung.xime.settings

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class LearnedCandidateIndexTest {

    @Test
    fun `legacy english records migrate with their word as input code`() {
        assertEquals(
            LearnedCandidateEntry("hello world", "hello world", 7L),
            LearnedCandidateIndex.parse("hello world\t7"),
        )
        assertEquals(
            LearnedCandidateEntry("hello", "hello", 0L),
            LearnedCandidateIndex.parse("hello"),
        )
    }

    @Test
    fun `english and chinese learned candidates share one frequency scale`() {
        val chinese = LearnedCandidateIndex.learn(emptyList(), "zhongguo", "中国", false)
        val english = LearnedCandidateIndex.learn(chinese, "international", "international", false)

        assertEquals(2L, english.first { it.word == "international" }.score)
        assertEquals(1L, english.first { it.word == "中国" }.score)
        assertEquals(listOf("中国"), LearnedCandidateIndex.matching(english, "zh").map { it.word })
        assertEquals(
            listOf("international"),
            LearnedCandidateIndex.matching(english, "INTER").map { it.word },
        )
    }

    @Test
    fun `reselecting a learned candidate increases its shared score`() {
        val learned = LearnedCandidateIndex.learn(emptyList(), "nihao", "你好", false)
        val selectedAgain = LearnedCandidateIndex.learn(learned, "nihao", "你好", true)

        assertEquals(2L, selectedAgain.single().score)
        assertEquals(
            learned,
            LearnedCandidateIndex.learn(learned, "nihao", "你好", false),
        )
    }

    @Test
    fun `prefix matching uses input code and deduplicates words`() {
        val entries = listOf(
            LearnedCandidateEntry("zhongguo", "中国", 4L),
            LearnedCandidateEntry("zhong", "中国", 2L),
            LearnedCandidateEntry("zhu", "猪", 3L),
        )

        assertEquals(
            listOf("中国", "猪"),
            LearnedCandidateIndex.matching(entries, "zh").map { it.word },
        )
        assertTrue(LearnedCandidateIndex.matching(entries, "sh").isEmpty())
    }

    @Test
    fun `serialized records preserve input code word and score`() {
        val entry = LearnedCandidateEntry("nihao", "你好", 9L)

        assertEquals(entry, LearnedCandidateIndex.parse(LearnedCandidateIndex.serialize(entry)))
    }
}
