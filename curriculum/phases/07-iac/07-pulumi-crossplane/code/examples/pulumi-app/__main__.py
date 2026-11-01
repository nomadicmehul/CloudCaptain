import pulumi
import pulumi_random as random

config = pulumi.Config()
suffix_length = config.get_int("suffixLength") or 6

pet = random.RandomPet("pet", length=2)
suffix = random.RandomString("suffix", length=suffix_length, special=False, upper=False)
name = pulumi.Output.all(pet.id, suffix.result).apply(lambda parts: f"{parts[0]}-{parts[1]}")
port = random.RandomInteger("port", min=8000, max=8999, keepers={"name": name})

pulumi.export("name", name)
pulumi.export("port", port.result)
