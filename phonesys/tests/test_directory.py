from phonesys.directory import DirectoryPerson, resolve_transfer


PEOPLE = (
    DirectoryPerson("7002", "John Yoder"),
    DirectoryPerson("7003", "John Smith"),
    DirectoryPerson("7004", "Atlas Assistant"),
)


def test_non_transfer_conversation_is_ignored():
    assert not resolve_transfer("John is a common name.", PEOPLE).requested


def test_unique_full_name_wins():
    result = resolve_transfer("Please transfer me to John Yoder", PEOPLE)
    assert [p.extension for p in result.matches] == ["7002"]


def test_ambiguous_first_name_returns_every_match():
    result = resolve_transfer("Can I speak to John?", PEOPLE)
    assert [p.extension for p in result.matches] == ["7002", "7003"]


def test_unique_first_name_resolves_when_only_one_exists():
    result = resolve_transfer("I'd like to talk to John", PEOPLE[:1])
    assert [p.extension for p in result.matches] == ["7002"]


def test_unknown_requested_name_has_no_matches():
    result = resolve_transfer("Connect me to Alice", PEOPLE)
    assert result.requested
    assert result.matches == ()


def test_personal_assistant_resolves_to_atlas():
    result = resolve_transfer("Transfer me to my personal assistant", PEOPLE)
    assert [p.full_name for p in result.matches] == ["Atlas Assistant"]
