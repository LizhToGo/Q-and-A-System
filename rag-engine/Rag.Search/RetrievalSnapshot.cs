using System.Collections.Frozen;
using System.Text;

namespace Rag.Search;

internal sealed class RetrievalSnapshot
{
    private readonly IndexedMaterial[] materials;
    private readonly FrozenDictionary<uint, int[]> postings;

    private RetrievalSnapshot(string revision, IndexedMaterial[] materials, FrozenDictionary<uint, int[]> postings)
    {
        Revision = revision;
        this.materials = materials;
        this.postings = postings;
    }

    public string Revision { get; }
    public int MaterialCount => materials.Length;
    public IndexedMaterial MaterialAt(int index) => materials[index];
    public bool TryPostings(uint gram, out int[] values) => postings.TryGetValue(gram, out values!);
    public static uint Gram(char first, char second) => ((uint)first << 16) | second;

    public static RetrievalSnapshot Create(SnapshotInput input)
    {
        if (input.Revision is null || input.Materials is null) throw new InvalidSnapshotException();
        var normalizedMaterials = new List<IndexedMaterial>(input.Materials.Length);
        var builders = new Dictionary<uint, List<int>>();
        var materialIds = new HashSet<int>();
        var chunkIds = new HashSet<int>();
        var buffer = new StringBuilder();
        foreach (MaterialInput material in input.Materials)
        {
            if (material is null || !materialIds.Add(material.Id) || material.MaterialType is null ||
                material.Title is null || material.Content is null || material.Tags is null ||
                material.Section is null || material.SourceAnchor is null || material.Chunks is null ||
                Array.Exists(material.Tags, static tag => tag is null))
                throw new InvalidSnapshotException();
            foreach (ChunkInput chunk in material.Chunks)
                if (chunk is null || chunk.Text is null || !chunkIds.Add(chunk.Id)) throw new InvalidSnapshotException();
            if (material.Revoked || material.Chunks.Length == 0) continue;

            // Spaces terminate Unicode case context, so fields can be lowered
            // directly into the final buffer without intermediate joined strings.
            buffer.Clear();
            PythonText.AppendLower(material.Title, buffer);
            buffer.Append(' ');
            PythonText.AppendLower(material.Content, buffer);
            buffer.Append(' ');
            for (int i = 0; i < material.Tags.Length; i++)
            {
                if (i > 0) buffer.Append(' ');
                PythonText.AppendLower(material.Tags[i], buffer);
            }
            buffer.Append(' ');
            PythonText.AppendLower(material.Section, buffer);
            string shared = buffer.ToString();
            var chunks = new IndexedChunk[material.Chunks.Length];
            for (int i = 0; i < chunks.Length; i++)
            {
                ChunkInput chunk = material.Chunks[i];
                chunks[i] = new IndexedChunk(chunk.Id, chunk.ChunkIndex, PythonText.Lower(chunk.Text, buffer));
            }
            string sourceKey = material.SourceAnchor.Length == 0 ? $"material:{material.Id}" : material.SourceAnchor;
            int materialIndex = normalizedMaterials.Count;
            normalizedMaterials.Add(new IndexedMaterial(material.Id, material.CourseId, material.TopicId,
                material.SourceDocumentId, material.MaterialType, material.Verified, sourceKey, shared, chunks));
            AddPostings(shared, materialIndex, builders);
            foreach (IndexedChunk chunk in chunks) AddPostings(chunk.Text, materialIndex, builders);
        }
        var packed = builders.ToFrozenDictionary(static entry => entry.Key, static entry => entry.Value.ToArray());
        return new RetrievalSnapshot(input.Revision, normalizedMaterials.ToArray(), packed);
    }

    private static void AddPostings(string text, int materialIndex, Dictionary<uint, List<int>> builders)
    {
        for (int offset = 0; offset + 1 < text.Length; offset++)
        {
            uint gram = Gram(text[offset], text[offset + 1]);
            if (!builders.TryGetValue(gram, out List<int>? values))
            {
                values = [];
                builders.Add(gram, values);
            }
            // Each material is indexed contiguously, so the tail is a free deduplication mark.
            if (values.Count == 0 || values[^1] != materialIndex) values.Add(materialIndex);
        }
    }
}

internal sealed record IndexedMaterial(int Id, int CourseId, int? TopicId, int? SourceDocumentId,
    string MaterialType, bool Verified, string SourceKey, string SharedText, IndexedChunk[] Chunks);

internal readonly record struct IndexedChunk(int Id, int Index, string Text);
