public partial class RampEmitter {
 public void Advance(int elapsed) {
  charge = System.Math.Max(0, charge - elapsed);
  cooldown = elapsed + 7;
 }
 public string Description() { return "charge cooldown PulseEmitter RampEmitter"; }
 public void ResetCharge() { charge = 0; }
 public void ResetCooldown() { cooldown = 0; }
}
