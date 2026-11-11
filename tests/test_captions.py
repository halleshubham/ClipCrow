from reelcore.captions import clip_words, group_words
from reelcore.edl import Word


def W(w, s, e):
    return Word(w=w, s=s, e=e)


def test_words_are_clipped_and_shifted_to_clip_time():
    t = [W("a", 0.0, 1.0), W("b", 5.0, 5.5), W("c", 9.8, 10.4), W("d", 12.0, 12.5)]
    out = clip_words(t, 5.0, 10.0)
    assert [(w.text, round(w.start, 2), round(w.end, 2), w.index) for w in out] == [
        ("b", 0.0, 0.5, 1), ("c", 4.8, 5.0, 2)]


def test_groups_respect_max_words():
    words = clip_words([W(str(i), i * .3, i * .3 + .25) for i in range(10)], 0, 10)
    groups = group_words(words, 10, max_words=4, pause_break=5)
    assert [len(g.words) for g in groups] == [4, 4, 2]


def test_pause_breaks_a_group():
    words = clip_words([W("a", 0.0, 0.3), W("b", 0.3, 0.6), W("c", 1.5, 1.8)], 0, 5)
    assert [len(g.words) for g in group_words(words, 5, pause_break=0.4)] == [2, 1]


def test_sentence_end_breaks_a_group_including_devanagari_danda():
    for end in (".", "?", "।", "॥"):
        words = clip_words([W("एक" + end, 0.0, 0.3), W("दो", 0.3, 0.6)], 0, 5)
        assert len(group_words(words, 5, pause_break=5)) == 2, end


def test_first_group_never_opens_at_zero():
    words = clip_words([W("a", 0.0, 0.4)], 0, 5)
    assert group_words(words, 5)[0].start >= 0.1


def test_groups_never_overlap_and_stay_inside_the_clip():
    words = clip_words([W(str(i), i * .2, i * .2 + .18) for i in range(30)], 0, 6)
    groups = group_words(words, 6, max_words=3, pause_break=0.05)
    for a, b in zip(groups, groups[1:]):
        assert a.end <= b.start + 1e-9
    assert groups[-1].end <= 6


def test_a_word_at_the_very_end_of_the_clip_is_dropped_not_negative():
    words = clip_words([W("a", 0.0, 0.5), W("late", 4.99, 5.2)], 0, 5)
    for g in group_words(words, 5):
        assert g.end > g.start
