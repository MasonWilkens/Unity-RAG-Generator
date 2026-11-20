namespace OrbitalGarden
{
    public class GreenhouseHeat
    {
        public const int SafeMinimum = 12;
        public bool NeedsHeat(int temperature)
        {
            return temperature < SafeMinimum;
        }
    }
}
