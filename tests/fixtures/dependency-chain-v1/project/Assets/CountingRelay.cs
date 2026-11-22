public static class CountingRelay {
 private static int routeCount;
 public static void Transfer(int value) {
  routeCount++;
  Records.RecordBook.Append(value);
 }
}
