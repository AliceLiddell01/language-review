"""Проверки метаданных Agent Skills и локальных ссылок Markdown."""

import os
import tempfile
import unittest
from pathlib import Path

from check_skill_contracts import validate_skill, validate_tree


class SkillContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / ".agents/skills/sample-skill"
        self.folder.mkdir(parents=True)
        self.file = self.folder / "SKILL.md"

    def write_skill(self, metadata=None, body="См. [описание](references/details.md)."):
        header = (
            metadata
            if metadata is not None
            else (
                "---\nname: sample-skill\ndescription: >-\n"
                "  Используй этот навык для конкретной задачи.\n---\n"
            )
        )
        self.file.write_text(header + body, encoding="utf-8")

    def frontmatter_with(self, entry):
        key = entry.partition(":")[0]
        values = {
            "name": "name: sample-skill",
            "description": "description: Описание навыка.",
        }
        values[key] = entry
        return "---\n" + "\n".join(values.values()) + "\n---\n"

    def test_valid_skill_and_local_reference(self):
        (self.folder / "references").mkdir()
        (self.folder / "references/details.md").write_text("Сведения", encoding="utf-8")
        self.write_skill()
        self.assertEqual(validate_tree(self.root), [])

    def test_reference_document_links_resolve_from_document_parent(self):
        references = self.folder / "references"
        references.mkdir()
        (references / "details.md").write_text("Сведения", encoding="utf-8")
        (references / "procedure.md").write_text("См. [описание](details.md).", encoding="utf-8")
        self.write_skill(body="Без ссылок.")
        self.assertEqual(validate_tree(self.root), [])

    def test_missing_link_in_reference_document_fails_tree_validation(self):
        references = self.folder / "references"
        references.mkdir()
        document = references / "procedure.md"
        document.write_text("См. [отсутствующее](missing.md).", encoding="utf-8")
        self.write_skill(body="Без ссылок.")
        errors = validate_tree(self.root)
        self.assertTrue(any(str(document) in error and "ссылка" in error for error in errors))

    def test_reference_document_link_cannot_escape_skill_directory(self):
        references = self.folder / "references"
        references.mkdir()
        outside = self.root / "outside.md"
        outside.write_text("Внешний файл", encoding="utf-8")
        document = references / "procedure.md"
        document.write_text("[внешний файл](../../../../outside.md)", encoding="utf-8")
        self.write_skill(body="Без ссылок.")
        errors = validate_tree(self.root)
        self.assertTrue(
            any(str(document) in error and "выходящая за skill" in error for error in errors)
        )

    def test_quoted_name_and_description_are_valid_strings(self):
        self.write_skill(
            '---\nname: "sample-skill"\ndescription: "Описание навыка."\n---\n',
            body="Без ссылок.",
        )
        self.assertEqual(validate_skill(self.file), [])

    def test_metadata_mapping_and_yaml_12_string_scalars_are_valid(self):
        self.write_skill(
            "---\nname: sample-skill\ndescription: yes\nlicense: on\n"
            'metadata: {author: example-org, version: "1.0"}\n---\n',
            body="Без ссылок.",
        )
        self.assertEqual(validate_skill(self.file), [])

    def test_invalid_name_and_nonstandard_routing_key(self):
        self.write_skill(
            "---\nname: wrong-name\ndescription: Демонстрация навыка.\nwhenToUse: never\n---\n",
            body="Без ссылок.",
        )
        errors = validate_skill(self.file)
        self.assertTrue(any("name" in error for error in errors))
        self.assertTrue(any("whenToUse" in error for error in errors))

    def test_invalid_yaml_types_are_rejected_for_all_typed_fields(self):
        cases = (
            ("name: [sample-skill]", "поле name должно быть строкой"),
            ("description: [обзор, git]", "поле description должно быть строкой"),
            ("license: {name: example}", "поле license должно быть строкой"),
            ("compatibility: 42", "поле compatibility должно быть строкой"),
            ("metadata: [a, b]", "metadata должно быть отображением"),
            ("metadata: {author: 42}", "metadata должны быть строками"),
            ("allowed-tools: [Read, Write]", "поле allowed-tools должно быть строкой"),
        )
        for entry, message in cases:
            with self.subTest(entry=entry):
                self.write_skill(self.frontmatter_with(entry), body="Без ссылок.")
                self.assertTrue(any(message in error for error in validate_skill(self.file)))

    def test_empty_required_and_constrained_values_are_rejected(self):
        cases = (
            ('description: ""', "description должен содержать"),
            ("description: null", "поле description должно быть строкой"),
            ('compatibility: ""', "compatibility должен содержать"),
            ("compatibility: ~", "поле compatibility должно быть строкой"),
        )
        for entry, message in cases:
            with self.subTest(entry=entry):
                self.write_skill(self.frontmatter_with(entry), body="Без ссылок.")
                self.assertTrue(any(message in error for error in validate_skill(self.file)))

    def test_missing_reference(self):
        self.write_skill()
        self.assertTrue(any("ссылка" in error for error in validate_skill(self.file)))

    def test_fenced_code_inline_code_and_escaped_links_are_ignored(self):
        body = (
            "```markdown\n[Пример](несуществующий-файл.md)\n```\n\n"
            "Код ` [пример](такого-файла-нет.md) ` и экранированная ссылка "
            "\\[пример](ещё-один-файл.md)."
        )
        self.write_skill(body=body)
        self.assertEqual(validate_skill(self.file), [])

    def test_nested_parentheses_and_markdown_title_are_parsed(self):
        target = self.folder / "references/file(name).md"
        target.parent.mkdir()
        target.write_text("Сведения", encoding="utf-8")
        self.write_skill(body='См. [документ](references/file(name).md "Название").')
        self.assertEqual(validate_skill(self.file), [])

    def test_reference_style_links_and_images_are_checked(self):
        target = self.folder / "references/details.md"
        target.parent.mkdir()
        target.write_text("Сведения", encoding="utf-8")
        self.write_skill(
            body="[описание][details]\n\n![схема](references/details.md)\n\n"
            "[details]: references/details.md?view=full#section"
        )
        self.assertEqual(validate_skill(self.file), [])
        self.write_skill(body="![отсутствующая схема](references/missing.png)")
        self.assertTrue(any("ссылка" in error for error in validate_skill(self.file)))

    def test_link_cannot_escape_skill_directory(self):
        outside = self.root / "outside.md"
        outside.write_text("Внешний файл", encoding="utf-8")
        self.write_skill(body="[ссылка](../outside.md)")
        self.assertTrue(any("выходящая за skill" in error for error in validate_skill(self.file)))

    def test_symlink_cannot_escape_skill_directory(self):
        outside = self.root / "outside.md"
        outside.write_text("Внешний файл", encoding="utf-8")
        link = self.folder / "outside.md"
        try:
            os.symlink(outside, link)
        except OSError, NotImplementedError:
            self.skipTest("Создание символических ссылок недоступно")
        self.write_skill(body="[ссылка](outside.md)")
        self.assertTrue(any("выходящая за skill" in error for error in validate_skill(self.file)))

    def test_description_and_compatibility_limits(self):
        for description, valid in (("x" * 1024, True), ("x" * 1025, False)):
            with self.subTest(description_length=len(description)):
                self.write_skill(
                    "---\nname: sample-skill\ndescription: |-\n  " + description + "\n---\n",
                    body="Без ссылок.",
                )
                self.assertEqual(validate_skill(self.file) == [], valid)
        for compatibility, valid in (("x" * 500, True), ("x" * 501, False)):
            with self.subTest(compatibility_length=len(compatibility)):
                self.write_skill(
                    "---\nname: sample-skill\ndescription: Описание навыка.\n"
                    f"compatibility: {compatibility}\n---\n",
                    body="Без ссылок.",
                )
                self.assertEqual(validate_skill(self.file) == [], valid)

    def test_yaml_comments_and_block_scalars_are_supported(self):
        self.write_skill(
            "---\nname: sample-skill # комментарий\n"
            'description: "Знак # остаётся частью значения" # комментарий\n'
            "compatibility: >-\n  Требуется доступ к системе.\n"
            'metadata:\n  author: example-org\n  version: "1.0"\n---\n',
            body="Без ссылок.",
        )
        self.assertEqual(validate_skill(self.file), [])

    def test_duplicate_keys_and_invalid_escapes_are_rejected(self):
        cases = (
            '---\nname: sample-skill\ndescription: "Некорректный \\q escape"\n---\n',
            "---\nname: sample-skill\ndescription: Описание.\nname: sample-skill\n---\n",
            "---\nname: sample-skill\ndescription: Описание.\n"
            "metadata:\n  author: first\n  author: second\n---\n",
            "---\nname: sample-skill\ndescription: [broken\n---\n",
        )
        for frontmatter in cases:
            with self.subTest(frontmatter=frontmatter):
                self.write_skill(frontmatter, body="Без ссылок.")
                self.assertTrue(
                    any("YAML frontmatter" in error for error in validate_skill(self.file))
                )

    def test_missing_frontmatter(self):
        self.write_skill("# Обычный Markdown", body="Без ссылок.")
        self.assertTrue(any("frontmatter" in error for error in validate_skill(self.file)))

    def test_directory_without_manifest_fails(self):
        self.write_skill(body="Без ссылок.")
        self.file.unlink()
        self.assertTrue(any("SKILL.md" in error for error in validate_tree(self.root)))


if __name__ == "__main__":
    unittest.main()
