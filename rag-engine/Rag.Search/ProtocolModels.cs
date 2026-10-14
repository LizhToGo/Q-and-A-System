namespace Rag.Search;

internal sealed class SearchRequest
{
    public SnapshotInput? Snapshot { get; init; }
    public required string Query { get; init; }
    public string[]? AdditionalQueries { get; init; }
    public int? CourseId { get; init; }
    public int? TopicId { get; init; }
    public int? SourceDocumentId { get; init; }
    public string[]? MaterialTypes { get; init; }
    public int[]? MaterialIds { get; init; }
    public int Limit { get; init; } = 8;
}

internal sealed class SnapshotInput
{
    public required string Revision { get; init; }
    public required MaterialInput[] Materials { get; init; }
}

internal sealed class MaterialInput
{
    public required int Id { get; init; }
    public required int CourseId { get; init; }
    public int? TopicId { get; init; }
    public int? SourceDocumentId { get; init; }
    public required string MaterialType { get; init; }
    public required string Title { get; init; }
    public required string Content { get; init; }
    public required string[] Tags { get; init; }
    public required string Section { get; init; }
    public required string SourceAnchor { get; init; }
    public required bool Verified { get; init; }
    public required bool Revoked { get; init; }
    public required ChunkInput[] Chunks { get; init; }
}

internal sealed class ChunkInput
{
    public required int Id { get; init; }
    public required int ChunkIndex { get; init; }
    public required string Text { get; init; }
}

internal readonly record struct SearchMatch(int MaterialId, int ChunkId, long Score, bool Verified, string SourceKey);

internal sealed class InvalidSnapshotException : Exception { }
