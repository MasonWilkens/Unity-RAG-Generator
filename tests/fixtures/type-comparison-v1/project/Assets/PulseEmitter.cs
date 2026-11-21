public partial class PulseEmitter {
 public void Tick(int elapsed) {
  charge += elapsed;
  cooldown = 2 * elapsed;
 }
 public string Tick(string message) {
  // charge and cooldown increase together in this obsolete description.
  return "charge cooldown PulseEmitter RampEmitter" + message;
 }
 public void ResetCharge() { charge = 0; }
 public void ResetCooldown() { cooldown = 0; }
}
