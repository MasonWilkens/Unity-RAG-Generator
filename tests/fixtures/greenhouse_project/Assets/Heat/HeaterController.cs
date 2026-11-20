namespace OrbitalGarden
{
    public class HeaterController
    {
        private readonly GreenhouseHeat heat = new GreenhouseHeat();
        public bool EnableHeater(int temperature)
        {
            return heat.NeedsHeat(temperature);
        }
    }
}
