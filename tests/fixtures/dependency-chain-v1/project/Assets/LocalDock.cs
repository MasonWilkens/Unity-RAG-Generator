public class LocalDock {
 public static void Run(int shipment) {
  if (shipment > 0) Forward(shipment);
 }
 private static void Forward(int value) {
  Records.RecordBook.Append(value);
 }
}
