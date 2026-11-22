namespace Records {
 public static partial class RecordBook {
  private static int auditStamp, obsoleteStamp;
  public static void Append(int payload) {
   auditStamp++;
  }
  public static void Append(string payload) { obsoleteStamp++; }
 }
}
namespace Decoys {
 public static class RecordBook {
  private static int auditStamp;
  public static void Append(int payload) { auditStamp += 100; }
 }
}
