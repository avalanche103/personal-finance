from dataclasses import asdict, dataclass, field


@dataclass
class ParseResult:
    records: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    artifacts: dict = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict:
        return {
            'records': self.records,
            'warnings': self.warnings,
            'metadata': self.metadata,
        }


class BaseImportParser:
    parser_name = 'base'
    supported_extensions: tuple[str, ...] = ()

    def parse(self, raw_import_file):
        raise NotImplementedError

    def persist(self, raw_import_file, result: ParseResult) -> int:
        return 0