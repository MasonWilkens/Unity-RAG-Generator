public class ThrottleMeter {
 private int charge, cooldown;
 public int Value { get { return charge + cooldown; } }
}
public class BrakeMeter {
 private int charge, cooldown;
 public int Value { get { return charge - cooldown; } }
}
