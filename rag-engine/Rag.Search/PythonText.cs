using System.Text;

namespace Rag.Search;

internal static class PythonText
{
    public static string Lower(ReadOnlySpan<char> value, StringBuilder buffer, bool normalizeWhitespace = false)
    {
        buffer.Clear();
        AppendLower(value, buffer, normalizeWhitespace);
        return buffer.ToString();
    }

    public static void AppendLower(ReadOnlySpan<char> value, StringBuilder buffer, bool normalizeWhitespace = false)
    {
        bool previousCased = false, pendingSpace = false;
        for (int offset = 0; offset < value.Length;)
        {
            int codePoint = ReadCodePoint(value, offset, out int width);
            offset += width;
            int lower;
            if (codePoint is >= 'A' and <= 'Z') lower = codePoint + 32;
            else if (codePoint == 0x3A3)
                lower = previousCased && !NextIsCased(value, offset) ? 0x3C2 : 0x3C3;
            else lower = codePoint < 128 ? codePoint : PythonUnicodeData.Lowercase(codePoint);

            if (codePoint < 128)
            {
                if (codePoint is >= 'A' and <= 'Z' or >= 'a' and <= 'z') previousCased = true;
                else if (codePoint is not (0x27 or 0x2E or 0x3A or 0x5E or 0x60)) previousCased = false;
            }
            else if (!PythonUnicodeData.IsCaseIgnorable(codePoint))
                previousCased = PythonUnicodeData.IsCased(codePoint);

            if (normalizeWhitespace && PythonUnicodeData.IsWhitespace(codePoint))
            {
                pendingSpace = buffer.Length > 0;
                continue;
            }
            if (pendingSpace)
            {
                buffer.Append(' ');
                pendingSpace = false;
            }
            if (codePoint == 0x130) buffer.Append("i\u0307");
            else AppendCodePoint(buffer, lower);
        }
    }

    public static void QueryTerms(string normalized, List<string> terms, HashSet<string> seen)
    {
        terms.Clear();
        seen.Clear();
        for (int offset = 0; offset < normalized.Length;)
        {
            int start = offset;
            int codePoint = ReadCodePoint(normalized, offset, out int width);
            bool chinese = codePoint is >= 0x4E00 and <= 0x9FFF;
            int length = 0;
            if (IsAsciiStart(codePoint))
            {
                offset++;
                length++;
                while (offset < normalized.Length && IsAsciiContinuation(normalized[offset]))
                {
                    offset++;
                    length++;
                }
            }
            else if (PythonUnicodeData.IsDecimal(codePoint))
            {
                ConsumeDigits(normalized, ref offset, ref length);
                if (offset < normalized.Length && normalized[offset] == '.' && offset + 1 < normalized.Length)
                {
                    int next = ReadCodePoint(normalized, offset + 1, out _);
                    if (PythonUnicodeData.IsDecimal(next))
                    {
                        offset++;
                        length++;
                        ConsumeDigits(normalized, ref offset, ref length);
                    }
                }
            }
            else if (chinese)
            {
                do
                {
                    offset++;
                    length++;
                } while (offset < normalized.Length && normalized[offset] is >= '\u4E00' and <= '\u9FFF');
            }
            else
            {
                offset += width;
                continue;
            }
            if (length <= 1) continue;
            AddTerm(normalized, start, offset - start, terms, seen);
            if (!chinese) continue;
            for (int gramWidth = 2; gramWidth <= 3; gramWidth++)
                for (int position = start; position + gramWidth <= offset; position++)
                    AddTerm(normalized, position, gramWidth, terms, seen);
        }
    }

    private static void AddTerm(string text, int start, int length, List<string> terms, HashSet<string> seen)
    {
        if (seen.GetAlternateLookup<ReadOnlySpan<char>>().Contains(text.AsSpan(start, length))) return;
        string term = start == 0 && length == text.Length ? text : text.Substring(start, length);
        seen.Add(term);
        terms.Add(term);
    }

    private static void ConsumeDigits(string text, ref int offset, ref int length)
    {
        while (offset < text.Length)
        {
            int codePoint = ReadCodePoint(text, offset, out int width);
            if (!PythonUnicodeData.IsDecimal(codePoint)) break;
            offset += width;
            length++;
        }
    }

    private static bool IsAsciiStart(int value) => value is >= 'a' and <= 'z' or '_';
    private static bool IsAsciiContinuation(int value) => IsAsciiStart(value) || value is >= '0' and <= '9';

    private static bool NextIsCased(ReadOnlySpan<char> text, int offset)
    {
        while (offset < text.Length)
        {
            int codePoint = ReadCodePoint(text, offset, out int width);
            if (!PythonUnicodeData.IsCaseIgnorable(codePoint)) return PythonUnicodeData.IsCased(codePoint);
            offset += width;
        }
        return false;
    }

    private static int ReadCodePoint(ReadOnlySpan<char> text, int offset, out int width)
    {
        char first = text[offset];
        if (char.IsHighSurrogate(first) && offset + 1 < text.Length && char.IsLowSurrogate(text[offset + 1]))
        {
            width = 2;
            return char.ConvertToUtf32(first, text[offset + 1]);
        }
        width = 1;
        return first;
    }

    private static void AppendCodePoint(StringBuilder buffer, int value)
    {
        if (value <= 0xFFFF) buffer.Append((char)value);
        else
        {
            int pair = value - 0x10000;
            buffer.Append((char)(0xD800 + (pair >> 10)));
            buffer.Append((char)(0xDC00 + (pair & 0x3FF)));
        }
    }
}
