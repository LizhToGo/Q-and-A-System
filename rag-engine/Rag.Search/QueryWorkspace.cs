using System.Text;

namespace Rag.Search;

internal sealed class QueryWorkspace
{
    private readonly StringBuilder textBuffer = new();
    private readonly List<string> terms = [];
    private readonly HashSet<string> termSet = new(StringComparer.Ordinal);
    private readonly List<int> candidates = [];
    private readonly List<SearchMatch> ranked = [];
    private readonly List<SearchMatch> selected = [];
    private readonly HashSet<string> sources = new(StringComparer.Ordinal);
    private readonly HashSet<string> materialTypes = new(StringComparer.Ordinal);
    private readonly HashSet<int> materialIds = [];
    private int[] marks = [];
    private byte[] sharedCounts = [];
    private int generation;

    public IReadOnlyList<SearchMatch> Search(RetrievalSnapshot snapshot, SearchRequest request)
    {
        selected.Clear();
        ranked.Clear();
        sources.Clear();
        candidates.Clear();
        if (request.Limit == 0) return selected;
        PythonText.QueryTerms(PythonText.Lower(request.Query, textBuffer, normalizeWhitespace: true), terms, termSet);
        if (terms.Count == 0) return selected;
        if (marks.Length < snapshot.MaterialCount) Array.Resize(ref marks, snapshot.MaterialCount);
        if (sharedCounts.Length < terms.Count) Array.Resize(ref sharedCounts, terms.Count);
        if (generation == int.MaxValue)
        {
            Array.Clear(marks);
            generation = 0;
        }
        generation++;
        materialTypes.Clear();
        if (request.MaterialTypes is not null)
            foreach (string type in request.MaterialTypes) materialTypes.Add(type);
        materialIds.Clear();
        if (request.MaterialIds is not null)
            foreach (int id in request.MaterialIds) materialIds.Add(id);

        foreach (string term in terms)
        {
            // A real occurrence contains every bigram. The rarest posting list is
            // an exact recall superset; union across terms preserves partial matches.
            int[]? rarest = null;
            for (int offset = 0; offset + 1 < term.Length; offset++)
            {
                if (!snapshot.TryPostings(RetrievalSnapshot.Gram(term[offset], term[offset + 1]), out int[] values))
                {
                    rarest = null;
                    break;
                }
                if (rarest is null || values.Length < rarest.Length) rarest = values;
            }
            if (rarest is null) continue;
            foreach (int materialIndex in rarest)
            {
                if (marks[materialIndex] == generation) continue;
                marks[materialIndex] = generation;
                IndexedMaterial material = snapshot.MaterialAt(materialIndex);
                if (MatchesFilters(material, request)) candidates.Add(materialIndex);
            }
        }
        foreach (int materialIndex in candidates)
        {
            IndexedMaterial material = snapshot.MaterialAt(materialIndex);
            long baseScore = 0;
            for (int i = 0; i < terms.Count; i++)
            {
                int count = CountCapped(material.SharedText, terms[i], 3);
                sharedCounts[i] = (byte)count;
                baseScore += 2 * count;
            }
            long bestScore = -1;
            IndexedChunk bestChunk = default;
            foreach (IndexedChunk chunk in material.Chunks)
            {
                long score = baseScore;
                for (int i = 0; i < terms.Count; i++)
                    if (sharedCounts[i] < 3)
                        score += 2 * CountCapped(chunk.Text, terms[i], 3 - sharedCounts[i]);
                if (score > bestScore || (score == bestScore &&
                    (chunk.Index < bestChunk.Index || (chunk.Index == bestChunk.Index && chunk.Id < bestChunk.Id))))
                {
                    bestScore = score;
                    bestChunk = chunk;
                }
            }
            if (bestScore > 0)
                ranked.Add(new SearchMatch(material.Id, bestChunk.Id, bestScore, material.Verified, material.SourceKey));
        }
        ranked.Sort(static (left, right) =>
        {
            int order = right.Score.CompareTo(left.Score);
            if (order != 0) return order;
            order = right.Verified.CompareTo(left.Verified);
            return order != 0 ? order : left.MaterialId.CompareTo(right.MaterialId);
        });
        foreach (SearchMatch match in ranked)
        {
            if (!sources.Add(match.SourceKey)) continue;
            selected.Add(match);
            if (request.Limit >= 0 && selected.Count >= request.Limit) break;
        }
        if (request.Limit < 0)
        {
            int retained = Math.Max(0, selected.Count + request.Limit);
            selected.RemoveRange(retained, selected.Count - retained);
        }
        return selected;
    }

    private bool MatchesFilters(IndexedMaterial material, SearchRequest request) =>
        (!request.CourseId.HasValue || material.CourseId == request.CourseId.Value) &&
        (!request.TopicId.HasValue || material.TopicId == request.TopicId.Value) &&
        (!request.SourceDocumentId.HasValue || material.SourceDocumentId == request.SourceDocumentId.Value) &&
        (materialTypes.Count == 0 || materialTypes.Contains(material.MaterialType)) &&
        (materialIds.Count == 0 || materialIds.Contains(material.Id));

    private static int CountCapped(string text, string term, int cap)
    {
        int count = 0, offset = 0;
        while (count < cap)
        {
            int match = text.IndexOf(term, offset, StringComparison.Ordinal);
            if (match < 0) break;
            count++;
            offset = match + term.Length;
        }
        return count;
    }
}
