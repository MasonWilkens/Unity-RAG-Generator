public static partial class ParcelRelay {
 public static void Transfer(int value) {
  Records.RecordBook.Append(value);
 }
 public static void Transfer(string value) { }
}
